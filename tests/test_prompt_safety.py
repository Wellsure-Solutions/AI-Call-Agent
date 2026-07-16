from app.core.prompts import PROMPT


def test_prompt_does_not_include_speakable_store_commands():
    assert "Store:" not in PROMPT
    assert "Set:" not in PROMPT
    assert "store[" not in PROMPT
    assert "interested=yes" not in PROMPT


def test_lead_context_treats_google_maps_business_name_as_brand_context():
    config_source = __import__("pathlib").Path("app/integrations/deepgram/config.py").read_text()

    assert "Google Maps" in config_source
    assert "brand/trading name" in config_source
    assert "Category: Not provided" in config_source
