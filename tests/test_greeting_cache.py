from __future__ import annotations

import asyncio

import pytest

from tests import fixtures
from app.core import settings
from app.integrations.deepgram.config import get_agent_settings
from app.telephony.audio.greeting_cache import (
    greeting_fingerprint,
    load_greeting,
    save_greeting,
)
from app.telephony.adapters.twilio_adapter import TwilioAdapter
from app.telephony.audio.paced_sender import PacedSender


def agent(settings_object) -> dict:
    return settings_object.dict()["agent"]


# ---------------------------------------------------------------------------
# The cache key
# ---------------------------------------------------------------------------
def test_every_input_that_changes_the_sound_changes_the_key():
    """A stale greeting is worse than none: it plays the old line, or the
    right line in the wrong voice, one sentence before the model speaks."""
    base = ("नमस्ते", "eleven_labs", "eleven_flash_v2_5", "voice-a", "hi")
    fingerprint = greeting_fingerprint(*base)
    for index, changed in enumerate(("different text", "cartesia", "eleven_turbo_v2_5", "voice-b", "en")):
        variant = list(base)
        variant[index] = changed
        assert greeting_fingerprint(*variant) != fingerprint


def test_the_same_configuration_is_stable_across_calls():
    base = ("नमस्ते", "eleven_labs", "eleven_flash_v2_5", "voice-a", "hi")
    assert greeting_fingerprint(*base) == greeting_fingerprint(*base)


# ---------------------------------------------------------------------------
# Loading, and refusing to load
# ---------------------------------------------------------------------------
def test_a_missing_cache_is_not_an_error(tmp_path):
    """A miss must fall back to the provider synthesising the greeting, not
    fail the call."""
    assert load_greeting(tmp_path, "nothing-here") is None
    assert load_greeting(None, "nothing-here") is None


def test_a_saved_greeting_round_trips(tmp_path):
    audio = b"\xff" * 8000
    save_greeting(tmp_path, "abc", audio)
    assert load_greeting(tmp_path, "abc") == audio


def test_a_truncated_render_is_rejected_rather_than_played(tmp_path):
    """A partial file is a failed render. Playing 40ms of a greeting to a
    customer is worse than waiting for the provider."""
    save_greeting(tmp_path, "abc", b"\xff" * 300)
    assert load_greeting(tmp_path, "abc") is None


def test_an_implausibly_long_file_is_rejected(tmp_path):
    save_greeting(tmp_path, "abc", b"\xff" * (8000 * 31))
    assert load_greeting(tmp_path, "abc") is None


# ---------------------------------------------------------------------------
# Suppressing the provider greeting
# ---------------------------------------------------------------------------
def test_the_provider_greeting_is_suppressed_when_we_play_our_own():
    """Both would otherwise be spoken, and the customer would hear the
    agent introduce itself twice."""
    assert agent(get_agent_settings(None, "twilio"))["greeting"] == settings.DEEPGRAM_GREETING
    assert agent(get_agent_settings(None, "twilio", greeting_already_played=True))["greeting"] is None


def test_the_model_is_told_what_the_customer_already_heard():
    """Suppressing the greeting without saying so leaves the model believing
    it has not spoken yet, so it opens by greeting again."""
    prompt = agent(get_agent_settings(None, "twilio", greeting_already_played=True))["think"]["prompt"]
    assert "ALREADY SPOKEN" in prompt
    assert settings.DEEPGRAM_GREETING in prompt
    assert "Do not greet again" in prompt


def test_the_note_is_absent_when_the_provider_speaks_the_greeting():
    prompt = agent(get_agent_settings(None, "twilio"))["think"]["prompt"]
    assert "ALREADY SPOKEN" not in prompt


def test_lead_context_survives_alongside_the_greeting_note():
    prompt = agent(get_agent_settings(
        {"business_name": "Sharma Electronics"}, "twilio", greeting_already_played=True
    ))["think"]["prompt"]
    assert "UNTRUSTED LEAD DATA" in prompt
    assert "ALREADY SPOKEN" in prompt


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------
class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)


def test_the_greeting_is_queued_before_the_sender_task_starts():
    """The whole point is that the first frame leaves on the next tick
    rather than after a websocket handshake and speech synthesis."""
    greeting = b"".join(fixtures.frames(fixtures.tone(8000, ms=200)))

    class Bridge:
        async def next_output(self):
            await asyncio.sleep(3600)  # never produces anything

    async def scenario() -> int:
        adapter = TwilioAdapter(audio_bridge=Bridge(), client=object())
        adapter.websocket = FakeWebSocket()
        adapter.stream_sid = "MZ"
        adapter.pending_greeting = greeting
        task = asyncio.create_task(adapter._send_to_twilio())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return len(adapter.websocket.sent)

    assert asyncio.run(scenario()) > 0, "greeting audio must start flowing immediately"


def test_no_greeting_configured_changes_nothing():
    class Bridge:
        async def next_output(self):
            await asyncio.sleep(3600)

    async def scenario() -> int:
        adapter = TwilioAdapter(audio_bridge=Bridge(), client=object())
        adapter.websocket = FakeWebSocket()
        adapter.stream_sid = "MZ"
        task = asyncio.create_task(adapter._send_to_twilio())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return len(adapter.websocket.sent)

    assert asyncio.run(scenario()) == 0


# ---------------------------------------------------------------------------
# Letting the closing line finish
# ---------------------------------------------------------------------------
def test_hangup_waits_for_audio_already_handed_to_twilio():
    """The reported bug: calls ended before the closing line finished.

    The close signal is AgentAudioDone, which means the provider stopped
    *sending*. Pacing keeps several seconds in flight after that, so hanging
    up on the signal cut the goodbye off mid-word.
    """
    adapter = TwilioAdapter(audio_bridge=object(), client=object())
    adapter.websocket = FakeWebSocket()
    adapter.stream_sid = "MZ"
    adapter._paced_sender = PacedSender(lambda _f: None)
    adapter._paced_sender.feed(b"\xff" * 4000)  # half a second still queued

    async def scenario() -> float:
        started = asyncio.get_running_loop().time()
        drain = asyncio.create_task(adapter._drain_playback(timeout=2.0))
        await asyncio.sleep(0.2)
        assert not drain.done(), "must still be waiting while audio is queued"
        adapter._paced_sender.discard()
        adapter._pending_marks.clear()
        await drain
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(scenario()) >= 0.2


def test_hangup_is_not_delayed_when_nothing_is_playing():
    adapter = TwilioAdapter(audio_bridge=object(), client=object())
    adapter._paced_sender = PacedSender(lambda _f: None)
    assert adapter.audio_currently_playing is False
    asyncio.run(adapter._drain_playback(timeout=5.0))


def test_the_drain_is_bounded_when_the_customer_already_hung_up():
    """No mark ever comes back from a dead line, so an unbounded wait would
    hold the call and its concurrency slot open indefinitely."""
    adapter = TwilioAdapter(audio_bridge=object(), client=object())
    adapter._paced_sender = PacedSender(lambda _f: None)
    adapter._pending_marks.add("m1")  # never acknowledged

    async def scenario() -> None:
        await adapter._drain_playback(timeout=0.3)

    asyncio.run(scenario())
    assert adapter.audio_currently_playing is True, "still unplayed, but we stopped waiting"

