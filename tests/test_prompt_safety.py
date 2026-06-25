from app.core.prompts import PROMPT


def test_prompt_does_not_include_speakable_store_commands():
    assert "Store:" not in PROMPT
    assert "Set:" not in PROMPT
    assert "store[" not in PROMPT
    assert "interested=yes" not in PROMPT
