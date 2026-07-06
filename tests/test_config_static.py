from pathlib import Path


def test_elevenlabs_deepgram_settings_do_not_send_unsupported_voice_settings():
    config_source = Path("app/integrations/deepgram/config.py").read_text()
    assert "DEEPGRAM_SPEAK_VOICE_ID" in config_source
    assert "voice_settings" not in config_source
