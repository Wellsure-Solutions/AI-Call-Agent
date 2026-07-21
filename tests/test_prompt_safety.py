from app.core.prompts import PROMPT
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
    assert "the business named in LEAD_DATA से बात हो रही है ना?" in prompt
    assert '"business_name": "Sharma Electronics"' in prompt
    assert '"category": "Mobile Accessories"' in prompt
    assert '"notes": "Owner prefers an evening callback"' in prompt


def test_lead_context_does_not_leak_business_placeholder_when_name_is_missing():
    prompt = _lead_context_prompt({"category": "Retail", "notes": "New lead"})

    assert "{business_name}" not in prompt
    assert "the business named in LEAD_DATA से बात हो रही है ना?" in prompt


def test_prompt_uses_one_consistent_verified_identity():
    assert "You are Janvi" in PROMPT
    assert "Amazon Business Shopping Department" in PROMPT
    assert "stay in character as Priya" not in PROMPT


def test_default_greeting_is_natural_mixed_script_and_campaign_branded():
    settings_source = __import__("pathlib").Path("app/core/settings.py").read_text()
    assert "नमस्ते जी" in settings_source
    assert "Amazon Business Team" in settings_source
