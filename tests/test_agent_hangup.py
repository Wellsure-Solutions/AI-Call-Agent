from __future__ import annotations

"""How a call ends now that no end_call tool is registered.

The tool was removed with the seller campaign. A call therefore ends exactly
one way from the agent's side: it speaks a line that `is_terminal_assistant_text`
recognises, and the engine closes on the AgentAudioDone that follows.

That makes these patterns load-bearing in a way they were not before. A
closing they miss does not end the call -- it runs on to the maximum-call
deadline holding a concurrency slot. A closing they match too eagerly hangs
up on a customer mid-conversation. Both directions are pinned here.
"""

import types

from app.core.prompts import PROMPT
from app.integrations.deepgram.config import get_agent_settings
from app.services.call_control import (
    END_CALL_REASONS,
    is_closing_call_message,
    is_terminal_assistant_text,
    normalize_end_call_reason,
)


def think_block(settings) -> dict:
    return settings.dict()["agent"]["think"]


# ---------------------------------------------------------------------------
# Every closing the script can produce must be recognised
# ---------------------------------------------------------------------------
def closings_in_prompt() -> list[str]:
    """The quoted lines the prompt tells the model to end on."""
    import re

    lines = re.findall(r'"([^"\n]*(?:goodbye|Goodbye)[^"\n]*)"', PROMPT)
    assert lines, "the prompt must contain at least one quoted closing"
    return lines


def test_every_closing_the_prompt_teaches_is_recognised():
    """The one that motivated this: "Sure sir. Thank you for your time,
    goodbye." was not matched, so that call could only end on the deadline."""
    missed = [line for line in closings_in_prompt() if not is_terminal_assistant_text(line)]
    assert not missed, f"these closings would never end a call: {missed}"


def test_the_silence_closing_is_recognised_too():
    """Spoken by the idle path, and it has to close the call like any other."""
    from app.core.settings import IDLE_CLOSING_MESSAGE

    assert is_terminal_assistant_text(IDLE_CLOSING_MESSAGE)


def test_mid_call_speech_does_not_hang_up_on_the_seller():
    """A false positive drops the line mid-conversation, so the patterns stay
    biased toward missing a closure rather than inventing one."""
    for line in (
        "Thank you sir, aap batayiye kya concern hai?",
        "Samajh gayi sir, thank you. Aapko returns ka concern hai?",
        "Hamari team aapko call karegi, thank you so much for listening",
        "Sorry sir, ek baar repeat karenge?",
        "Koi particular reason hai jiski wajah se aap Amazon par start nahi karna chahte?",
        "",
    ):
        assert is_terminal_assistant_text(line) is False, line


# ---------------------------------------------------------------------------
# The provider close signal
# ---------------------------------------------------------------------------
def test_the_legacy_closing_marker_is_still_recognised():
    assert is_closing_call_message(
        types.SimpleNamespace(type="ConversationText"), "assistant: _closing_call()"
    ) is True


def test_ordinary_conversation_is_not_a_close_signal():
    assert is_closing_call_message(
        types.SimpleNamespace(type="ConversationText", content="Namaste ji")
    ) is False


def test_reason_parsing_never_raises_on_model_produced_arguments():
    """Still reachable: the reason is recorded on the session for the idle
    close and for reconciliation, and none of these may raise."""
    assert normalize_end_call_reason('{"reason": "not_interested"}') == "not_interested"
    assert normalize_end_call_reason('{"reason": "NOT_INTERESTED"}') == "not_interested"
    assert normalize_end_call_reason('{"reason": "made up"}') == "other"
    assert normalize_end_call_reason('{"reason": ""}') == "unspecified"
    assert normalize_end_call_reason('{"reason":') == "unparseable"
    assert normalize_end_call_reason("[1,2,3]") == "other"
    assert normalize_end_call_reason(None) == "unspecified"
    assert normalize_end_call_reason("") == "unspecified"


def test_the_reason_vocabulary_is_still_bounded():
    assert "not_interested" in END_CALL_REASONS
    assert "do_not_call" in END_CALL_REASONS


# ---------------------------------------------------------------------------
# What the settings message carries
# ---------------------------------------------------------------------------
def test_no_function_is_registered_on_the_agent():
    """Deliberate: the seller campaign closes on spoken text alone. Pinned so
    that re-adding a tool is a decision somebody makes on purpose, with the
    text-based close reconsidered at the same time.
    """
    assert "functions" not in think_block(get_agent_settings(None, "twilio"))


def test_settings_still_serialize_for_both_transports():
    import json

    for transport in ("twilio", "browser"):
        payload = json.dumps(
            get_agent_settings({"business_name": "Sharma Electronics"}, transport).dict(),
            default=str,
        )
        assert "UNTRUSTED LEAD DATA" in payload
        assert "Sharma Electronics" in payload
