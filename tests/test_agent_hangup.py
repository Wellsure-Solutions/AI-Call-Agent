from __future__ import annotations

import json
import types

from app.integrations.deepgram.config import get_agent_settings
from app.services.call_control import (
    END_CALL_FUNCTION,
    END_CALL_FUNCTION_SCHEMA,
    is_closing_call_message,
    is_terminal_assistant_text,
    normalize_end_call_reason,
)
from app.core.prompts import PROMPT


def think_block(settings) -> dict:
    return settings.dict()["agent"]["think"]


def test_the_end_call_function_is_actually_registered_on_the_agent():
    """The whole hangup path was previously dead because nothing registered a
    function: the detector existed, the tool did not."""
    functions = think_block(get_agent_settings({"business_name": "Test"}, "twilio"))["functions"]
    assert [f["name"] for f in functions] == [END_CALL_FUNCTION]


def test_the_end_call_function_is_client_side():
    """No `endpoint` is what makes Deepgram hand the call back to us rather
    than trying to reach an HTTP service that does not exist."""
    assert "endpoint" not in END_CALL_FUNCTION_SCHEMA


def test_the_function_is_registered_for_browser_calls_too():
    assert think_block(get_agent_settings(None, "browser"))["functions"]


def test_the_prompt_tells_the_model_the_tool_exists_and_when_to_use_it():
    """A registered tool the prompt never mentions is still a dead path."""
    assert END_CALL_FUNCTION in PROMPT
    for reason in ("not_interested", "do_not_call", "wrong_number", "voicemail"):
        assert reason in PROMPT


def test_the_prompt_forbids_speaking_the_tool_name_aloud():
    """Everything the model emits is synthesised to audio, so a leaked tool
    name is heard by the customer."""
    # Normalised because the instruction wraps across lines in the prompt.
    flattened = " ".join(PROMPT.lower().split())
    assert 'never say the words "end call" or "end_call" out loud' in flattened


def test_every_reason_the_prompt_names_exists_in_the_schema():
    """A reason the prompt teaches but the schema rejects would make the model
    produce an argument the enum refuses."""
    enum = set(END_CALL_FUNCTION_SCHEMA["parameters"]["properties"]["reason"]["enum"])
    for reason in ("completed", "not_interested", "do_not_call", "wrong_number", "callback_later", "voicemail"):
        assert reason in enum
        assert reason in PROMPT


def test_a_function_call_message_is_recognised_as_terminal():
    assert is_closing_call_message(types.SimpleNamespace(name=END_CALL_FUNCTION)) is True


def test_the_legacy_closing_marker_is_still_recognised():
    assert is_closing_call_message(types.SimpleNamespace(type="ConversationText"), "assistant: _closing_call()") is True


def test_ordinary_conversation_is_not_treated_as_terminal():
    assert is_closing_call_message(types.SimpleNamespace(type="ConversationText", content="Namaste ji")) is False


def test_reason_parsing_never_raises_on_model_produced_arguments():
    """The arguments are a JSON string the model wrote, so every one of these
    is reachable. None of them may stop a hangup."""
    assert normalize_end_call_reason('{"reason": "not_interested"}') == "not_interested"
    assert normalize_end_call_reason('{"reason": "NOT_INTERESTED"}') == "not_interested"
    assert normalize_end_call_reason('{"reason": "made up"}') == "other"
    assert normalize_end_call_reason('{"reason": ""}') == "unspecified"
    assert normalize_end_call_reason('{"reason":') == "unparseable"
    assert normalize_end_call_reason("[1,2,3]") == "other"
    assert normalize_end_call_reason(None) == "unspecified"
    assert normalize_end_call_reason("") == "unspecified"


# ---------------------------------------------------------------------------
# The Devanagari backstop. The prompt mandates Devanagari, so the previous
# romanized-only list could never fire on a real call.
# ---------------------------------------------------------------------------
def test_devanagari_closings_are_detected():
    assert is_terminal_assistant_text("ठीक है सर, आपका दिन शुभ हो!") is True
    assert is_terminal_assistant_text("धन्यवाद जी, नमस्ते") is True
    assert is_terminal_assistant_text("हमारी team आपको कॉल करेंगे") is True


def test_romanized_closings_still_work():
    assert is_terminal_assistant_text("Aapka din shubh ho!") is True
    assert is_terminal_assistant_text("The team will reach out soon.") is True


def test_mid_call_devanagari_does_not_trigger_a_hangup():
    """A false positive hangs up on a customer mid-sentence, so the backstop
    must stay biased toward missing a closure rather than inventing one."""
    for line in (
        "क्या आप Amazon पे shopping करते हैं?",
        "धन्यवाद, तो सर मैं आपको benefits बताती हूँ",
        "जी हाँ, GST invoice मिलता है",
        "Business account पर दस प्रतिशत cashback मिलता है",
        "नमस्ते सर, मैं Shruti बोल रही हूँ",
        "",
    ):
        assert is_terminal_assistant_text(line) is False, line


def test_settings_still_serialize_with_the_function_attached():
    """Deepgram rejects unknown fields, so the settings object has to survive
    the SDK's own serialization with functions present."""
    payload = json.dumps(get_agent_settings({"business_name": "Sharma Electronics"}, "twilio").dict(), default=str)
    assert END_CALL_FUNCTION in payload
    # The untrusted-lead-data framing must survive alongside it.
    assert "UNTRUSTED LEAD DATA" in payload
