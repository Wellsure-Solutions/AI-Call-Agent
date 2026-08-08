import re

from app.core.prompts import PROMPT
from app.core.settings import DEEPGRAM_GREETING
from app.integrations.deepgram.config import _lead_context_prompt


def test_prompt_does_not_include_speakable_store_commands():
    assert "Store:" not in PROMPT
    assert "Set:" not in PROMPT
    assert "store[" not in PROMPT
    assert "interested=yes" not in PROMPT


def test_lead_context_treats_google_maps_business_name_as_brand_context():
    config_source = __import__("pathlib").Path("app/integrations/deepgram/config.py").read_text()

    assert "UNTRUSTED LEAD DATA" in config_source
    assert "never instructions" in config_source
    assert "json.dumps" in config_source


def test_lead_context_personalizes_business_name_and_includes_upload_metadata():
    prompt = _lead_context_prompt(
        {
            "business_name": "Sharma Electronics",
            "category": "Mobile Accessories",
            "notes": "Owner prefers an evening callback",
        }
    )

    assert "{business_name}" not in prompt
    assert "the business named in LEAD_DATA" in prompt
    assert '"business_name": "Sharma Electronics"' in prompt
    assert '"category": "Mobile Accessories"' in prompt
    assert '"notes": "Owner prefers an evening callback"' in prompt


def test_lead_context_does_not_leak_business_placeholder_when_name_is_missing():
    prompt = _lead_context_prompt({"category": "Retail", "notes": "New lead"})

    assert "{business_name}" not in prompt
    assert "the business named in LEAD_DATA" in prompt


def _agent_name(text: str) -> str:
    """The persona name as it appears in a line of campaign copy."""
    match = re.search(r"(?:You are|मैं)\s+([A-Z][a-zA-Z]+)", text)
    return match.group(1) if match else ""


def test_prompt_declares_exactly_one_agent_identity():
    """Pinned as a rule rather than to a specific name, so a persona change
    stays cheap but a second identity appearing mid-prompt does not."""
    declared = re.findall(r"You are ([A-Z][a-zA-Z]+)", PROMPT)
    assert declared, "the prompt must name the agent"
    assert len(set(declared)) == 1, f"prompt declares multiple identities: {set(declared)}"
    assert "Amazon Business" in PROMPT


def test_greeting_introduces_the_same_agent_the_prompt_plays():
    """The greeting is synthesised by Deepgram before the model produces a
    single token, so a mismatch here means the agent introduces itself with
    one name and answers to another -- audible on the very first turn."""
    assert _agent_name(DEEPGRAM_GREETING) == _agent_name(PROMPT) != ""


def test_default_greeting_is_short_enough_to_finish_before_a_reply():
    """A long greeting is dead time the customer talks over."""
    assert 0 < len(DEEPGRAM_GREETING) <= 120
