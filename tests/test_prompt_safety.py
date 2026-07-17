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


def test_prompt_uses_one_consistent_verified_identity():
    assert "You are Vaibhav" in PROMPT
    assert "WellSure" in PROMPT
    assert "registered Seller Affiliate" in PROMPT
    assert "You do not work as an Amazon employee" in PROMPT
    assert "stay in character as Priya" not in PROMPT
    assert "1000 of orders" not in PROMPT


def test_default_greeting_is_natural_mixed_script_and_wellsure_branded():
    settings_source = __import__("pathlib").Path("app/core/settings.py").read_text()

    assert "नमस्ते जी" in settings_source
    assert "WellSure" in settings_source
    assert "registered Seller Affiliate" in settings_source
    assert "main vaibhav baat kar raha hoon Amazon se" not in settings_source