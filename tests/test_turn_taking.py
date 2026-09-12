from __future__ import annotations

from pathlib import Path

from app.core import settings


def test_eager_eot_is_optional(monkeypatch):
    settings_source = Path("app/core/settings.py").read_text()
    # The value itself is a tuning decision; what must hold is that it is
    # env-overridable and that eager mode stays separately controllable.
    assert 'os.getenv("DEEPGRAM_EOT_THRESHOLD"' in settings_source

    monkeypatch.delenv("TEST_EAGER_EOT", raising=False)
    assert settings._optional_float("TEST_EAGER_EOT") is None

    monkeypatch.setenv("TEST_EAGER_EOT", "")
    assert settings._optional_float("TEST_EAGER_EOT") is None

    monkeypatch.setenv("TEST_EAGER_EOT", "0.6")
    assert settings._optional_float("TEST_EAGER_EOT") == 0.6


def test_listen_settings_omit_eager_eot_when_disabled():
    source = Path("app/integrations/deepgram/config.py").read_text()
    assert '"eot_threshold": DEEPGRAM_EOT_THRESHOLD' in source
    assert "if DEEPGRAM_EAGER_EOT_THRESHOLD is not None:" in source
    assert 'provider["eager_eot_threshold"] = DEEPGRAM_EAGER_EOT_THRESHOLD' in source


def test_user_started_speaking_notifies_interruption_callback():
    source = Path("app/core/conversation/conversation_engine.py").read_text()
    interruption_branch = source.split('if msg_type == "UserStartedSpeaking":', maxsplit=1)[1]
    interruption_branch = interruption_branch.split("if is_closing_call_message", maxsplit=1)[0]
    assert "self._call_threadsafe(self.on_interrupted)" in interruption_branch


def test_bridge_discards_buffered_audio_on_interruption():
    source = Path("app/telephony/audio/audio_bridge.py").read_text()
    handler = source.split("def _handle_interruption", maxsplit=1)[1]
    handler = handler.split("def _mark_finished", maxsplit=1)[0]
    # "pause" joined the discard set when soft barge-in landed: a stale pause
    # left queued behind a committed interruption would re-pause the very next
    # reply the agent produced.
    assert 'message[0] not in {"audio", "interrupt", "pause"}' in handler
    assert '("interrupt", \'{"event": "user_started_speaking"}\')' in handler


def test_outbound_pump_clears_playback_on_interruption():
    # NB: this asserts on file *contents*, not on behaviour. The pump moved
    # from TwilioAdapter to the shared StreamingMediaAdapter when Exotel was
    # added -- both carriers run this exact loop now -- so only the path
    # changed here. Converting it to a behavioural test is worth doing, but
    # separately: landing it inside a provider refactor would make any
    # regression ambiguous in origin.
    source = Path("app/telephony/adapters/streaming_media.py").read_text()
    interruption_branch = source.split('elif message_type == "interrupt":', maxsplit=1)[1]
    assert "await self.clear_playback()" in interruption_branch.split("elif", maxsplit=1)[0]
