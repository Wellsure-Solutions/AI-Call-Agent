from pathlib import Path


def test_elevenlabs_deepgram_settings_do_not_send_unsupported_voice_settings():
    config_source = Path("app/integrations/deepgram/config.py").read_text()
    assert '"voice_id": "IpXGk4Ks434Jj33XXcNh"' in config_source
    assert "voice_settings" not in config_source
