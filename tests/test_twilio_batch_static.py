from pathlib import Path


TWILIO_ROUTES = Path("app/telephony/twilio_routes.py").read_text()
TWILIO_ADAPTER = Path("app/telephony/adapters/twilio_adapter.py").read_text()


def test_outbound_calls_do_not_prewarm_deepgram_before_media_stream_audio():
    start_outbound_section = TWILIO_ROUTES.split('@router.post("/twiml")')[0]
    assert "await bridge.start()" not in start_outbound_section
    assert "await adapter.connect()" in start_outbound_section


def test_status_webhook_does_not_start_deepgram_before_media_stream_audio():
    assert "await adapter.audio_bridge.start()" not in TWILIO_ROUTES
    assert "_record_twilio_status" in TWILIO_ROUTES


def test_twiml_adds_call_sid_stream_parameter_for_deterministic_mapping():
    assert "parameters={\"callSid\": call_sid}" in TWILIO_ROUTES
    assert "customParameters" in TWILIO_ROUTES
    assert "def build_twiml(stream_ws_url: str, parameters:" in TWILIO_ADAPTER
    assert "stream.parameter(name=name, value=value)" in TWILIO_ADAPTER
