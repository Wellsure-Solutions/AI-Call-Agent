from __future__ import annotations

"""Two ways a closing still went missing, from one batch.

Call A ended with no closing at all. The voice had been changed to a new
voice_id, which changes the cache key, so the pre-rendered goodbye no longer
resolved -- the fallback found nothing to play and the call closed in
silence. Nothing failed and nothing was logged loudly enough to notice.

Call B spoke its closing but the customer did not hear the end of it. The AI
stops accepting audio on Deepgram's listener thread, and the receive loop can
observe that *before* the audio callbacks queued ahead of it have reached the
paced sender -- so playback was measured while the sender was still empty and
read as "nothing left to play".
"""

import asyncio

import pytest

from app.core.conversation import conversation_engine as engine_module
from app.core.conversation.conversation_engine import ConversationEngine
from app.telephony.adapters.twilio_adapter import TwilioAdapter
from app.telephony.audio.greeting_cache import greeting_fingerprint, save_greeting
from app.telephony.audio.paced_sender import PacedSender
from app.telephony.call_session import CallSession
from app.telephony.metrics import CallMetrics


# ---------------------------------------------------------------------------
# Call A: the cache key moved and nobody noticed
# ---------------------------------------------------------------------------
def test_changing_the_voice_invalidates_the_rendered_audio(tmp_path):
    """This is the whole mechanism, and it is working as designed -- playing
    the old voice's goodbye after switching voices would be worse. What was
    missing is anyone being told."""
    text, provider, model = "धन्यवाद", "eleven_labs", "eleven_flash_v2_5"
    old = greeting_fingerprint(text, provider, model, "k2intd1ORm0YUH8etnXg", "hi")
    new = greeting_fingerprint(text, provider, model, "Ms9OTvWb99V6DwRHZn6q", "hi")
    assert old != new

    save_greeting(tmp_path, old, b"\x7f" * 24000, "closing")
    from app.telephony.audio.greeting_cache import load_greeting
    assert load_greeting(tmp_path, new, "closing") is None


def test_a_missing_closing_is_counted_not_just_skipped(monkeypatch):
    """It cost every call in a batch its goodbye and showed up nowhere but a
    recording."""
    monkeypatch.setattr(engine_module, "cached_closing_audio", lambda: None)
    metrics = CallMetrics("call-a")
    metrics.bind()
    engine = ConversationEngine(
        CallSession(call_id="call-a", direction="twilio"),
        lambda _f: None, lambda _t: None, lambda: None, metrics=metrics,
    )
    engine._assistant_spoke_since_user = False

    asyncio.run(engine._close_after_grace(0.01))

    summary = next(p for name, p in metrics.finish("completed") if name == "metrics_call")
    assert summary["closings_unavailable"] == 1
    assert summary["fallback_closings"] == 0
    assert engine.closing_requested is True, "a missing cache must not fail the call"


def test_startup_names_every_phrase_that_is_not_rendered(monkeypatch):
    """Caught before a batch is dialled rather than after it is reviewed."""
    import app.main as main
    import app.integrations.deepgram.config as config

    monkeypatch.setattr(config, "cached_greeting_audio", lambda: None)
    monkeypatch.setattr(config, "cached_closing_audio", lambda: None)
    missing = main.warn_about_unrendered_audio()
    assert len(missing) == 2
    assert any("greeting" in item for item in missing)
    assert any("closing" in item for item in missing)

    monkeypatch.setattr(config, "cached_greeting_audio", lambda: b"\x7f" * 24000)
    monkeypatch.setattr(config, "cached_closing_audio", lambda: b"\x7f" * 24000)
    assert main.warn_about_unrendered_audio() == []


# ---------------------------------------------------------------------------
# Call B: playback measured before the audio arrived
# ---------------------------------------------------------------------------
class Socket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)

    async def receive_text(self) -> str:
        await asyncio.sleep(0.02)
        return '{"event": "media", "media": {"payload": "//////////8="}}'

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


def test_the_close_waits_for_the_pump_to_take_the_goodbye():
    """The race: the close begins while the goodbye is still sitting in the
    bridge queue, so the paced sender is empty and playback reads as finished.
    Waiting for the close signal is exact -- it is queued behind the audio, so
    the pump reaching it proves the whole goodbye was handed over.
    """
    adapter = TwilioAdapter(audio_bridge=object(), client=object())
    adapter.websocket = Socket()
    adapter.stream_sid = "MZ"
    adapter._paced_sender = PacedSender(lambda _f: None)

    async def scenario() -> float:
        started = asyncio.get_running_loop().time()

        async def hand_over_late() -> None:
            # The pump gets its turn a little after the close begins.
            await asyncio.sleep(0.3)
            adapter._paced_sender.feed(b"\xff" * 8000)
            adapter._close_after_playback.set()

        asyncio.create_task(hand_over_late())
        await adapter._await_close_signal()
        handed_over = adapter.buffered_playback_seconds
        assert handed_over == pytest.approx(1.0), "must not measure before the handoff"
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(scenario()) >= 0.3


def test_the_wait_for_the_handoff_is_bounded():
    """A provider that died mid-turn never sends a close signal, and the call
    still has to end."""
    adapter = TwilioAdapter(audio_bridge=object(), client=object())
    adapter._paced_sender = PacedSender(lambda _f: None)

    async def scenario() -> float:
        started = asyncio.get_running_loop().time()
        await adapter._await_close_signal()
        return asyncio.get_running_loop().time() - started

    from app.core.settings import AGENT_PLAYBACK_HANDOFF_SECONDS
    elapsed = asyncio.run(scenario())
    assert AGENT_PLAYBACK_HANDOFF_SECONDS <= elapsed < AGENT_PLAYBACK_HANDOFF_SECONDS + 1.0


def test_a_signal_already_raised_costs_nothing():
    """The common case: the pump got there first, and the close proceeds with
    no added delay at all."""
    adapter = TwilioAdapter(audio_bridge=object(), client=object())
    adapter._close_after_playback.set()

    async def scenario() -> float:
        started = asyncio.get_running_loop().time()
        await adapter._await_close_signal()
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(scenario()) < 0.05
