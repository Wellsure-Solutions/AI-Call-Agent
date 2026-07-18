from pathlib import Path

from app.integrations.audio_profiles import get_audio_profile
from app.integrations.twilio_media import decode_media_payload, encode_media_payload


def test_twilio_uses_native_mulaw_at_eight_kilohertz():
    profile = get_audio_profile("twilio")

    assert profile.encoding == "mulaw"
    assert profile.input_sample_rate == 8000
    assert profile.output_sample_rate == 8000
    assert profile.container == "none"


def test_browser_keeps_existing_linear_pcm_profile():
    profile = get_audio_profile("browser")

    assert profile.encoding == "linear16"
    assert profile.input_sample_rate == 48000
    assert profile.output_sample_rate == 24000
    assert profile.container == "none"


def test_twilio_payload_round_trip_preserves_every_audio_byte():
    mulaw_frame = bytes(range(256)) * 2

    encoded = encode_media_payload(mulaw_frame)

    assert decode_media_payload(encoded) == mulaw_frame


def test_twilio_adapter_does_not_resample_or_transcode_frames():
    source = Path("app/telephony/adapters/twilio_adapter.py").read_text()

    assert "audioop" not in source
    assert "ratecv" not in source
    assert "lin2ulaw" not in source
    assert "ulaw2lin" not in source
    assert "encode_media_payload(mulaw_frame)" in source
    assert 'decode_media_payload(msg["media"]["payload"])' in source


def test_conversation_engine_selects_profile_from_session_direction():
    source = Path("app/core/conversation/conversation_engine.py").read_text()

    assert "transport=self.session.direction" in source


def test_deepgram_settings_apply_the_selected_audio_profile():
    source = Path("app/integrations/deepgram/config.py").read_text()

    assert "audio_profile = get_audio_profile(transport)" in source
    assert "encoding=audio_profile.encoding" in source
    assert "sample_rate=audio_profile.input_sample_rate" in source
    assert "sample_rate=audio_profile.output_sample_rate" in source