from __future__ import annotations

"""The goodbye must reach the customer before Twilio is told to hang up.

Two real calls, after the drain was already in place:

    "जी बिल्कुल सर, मैं note कर देती हूँ। धन्यवाद। आपका दिन शुभ हो।"
        -> heard as far as "जी बिल्कुल सर, मैं note"

    "जी बढ़िया सर, हमारी team आपको कल सुबह call करके पूरा process बता देगी।
     आपका दिन शुभ हो सर, धन्यवाद।"
        -> not a word of it was heard

The drain was correct and never ran. Two races beat it:

  1. `ConversationEngine.receive_audio` returns False as soon as the engine
     is closing, so within one 20ms frame of the provider finishing *sending*
     the goodbye the receive loop called `_complete_twilio_call()` -- telling
     Twilio to hang up while the paced sender still held nearly all of it.
  2. The drain lived in the outbound pump, and the receive loop's teardown
     cancelled that task; and breaking out of the pump closed the paced
     sender, discarding the queued goodbye outright.

Both are ordering problems, so these tests assert on order and on how much
audio actually left for Twilio.
"""

import asyncio
import json

import pytest

from app.telephony.adapters.twilio_adapter import TwilioAdapter
from app.telephony.audio.paced_sender import PacedSender


class FakeSocket:
    """A Twilio media stream that echoes marks back, as the real one does."""

    def __init__(self, *, echo_marks: bool = True, stop_after: float | None = None) -> None:
        self.sent: list[dict] = []
        self.echo_marks = echo_marks
        self.stop_after = stop_after
        self.closed = False
        self._inbound: asyncio.Queue[str] = asyncio.Queue()
        self._started = None

    async def send_text(self, payload: str) -> None:
        message = json.loads(payload)
        self.sent.append(message)
        if message.get("event") == "mark" and self.echo_marks:
            # The real Twilio echoes a mark once it has played the audio
            # preceding it. Immediate here; the pacing is what takes time.
            await self._inbound.put(payload)

    async def receive_text(self) -> str:
        loop = asyncio.get_running_loop()
        if self._started is None:
            self._started = loop.time()
        if self.stop_after is not None and loop.time() - self._started >= self.stop_after:
            return json.dumps({"event": "stop"})
        try:
            return await asyncio.wait_for(self._inbound.get(), timeout=0.02)
        except asyncio.TimeoutError:
            # Twilio delivers inbound media continuously, silence included.
            return json.dumps({"event": "media", "media": {"payload": "//////////8="}})

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    @property
    def media_frames(self) -> int:
        return sum(1 for m in self.sent if m.get("event") == "media")


class FakeCalls:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def __call__(self, _sid):
        return self

    def update(self, status: str) -> None:
        self._log.append(f"hangup:{status}")


def adapter_with_goodbye(seconds: float, socket: FakeSocket, log: list[str]) -> TwilioAdapter:
    client = type("Client", (), {"calls": FakeCalls(log)})()
    adapter = TwilioAdapter(audio_bridge=object(), client=client)
    adapter.websocket = socket
    adapter.stream_sid = "MZ"
    adapter.call_sid = "CA-test"
    adapter._paced_sender = PacedSender(adapter.send_audio, on_frame_sent=adapter._on_frame_sent)
    adapter._paced_sender.feed(b"\xff" * int(8000 * seconds))
    return adapter


# ---------------------------------------------------------------------------
# The reported failure
# ---------------------------------------------------------------------------
def test_the_whole_goodbye_is_sent_before_twilio_is_told_to_hang_up():
    """Six seconds of closing, and every frame of it must leave first."""
    log: list[str] = []
    socket = FakeSocket()
    adapter = adapter_with_goodbye(6.0, socket, log)

    async def scenario() -> None:
        pump = asyncio.create_task(adapter._paced_sender.run())
        await adapter._close_after_goodbye()
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert log == ["hangup:completed"], "the call must have been completed exactly once"
    assert socket.media_frames == int(8000 * 6.0) // 160, "every frame of the goodbye"
    assert adapter.buffered_playback_seconds == 0.0
    assert socket.closed is True


def test_the_hangup_comes_after_the_last_frame_not_before():
    """The precise ordering that was inverted. A hangup issued before the
    final frames is what the customer hears as being cut off."""
    log: list[str] = []
    socket = FakeSocket()
    adapter = adapter_with_goodbye(2.0, socket, log)

    real_update = FakeCalls(log).update

    def update(status: str) -> None:
        log.append(f"frames_sent:{socket.media_frames}")
        real_update(status)

    adapter._client.calls(None).update = update

    async def scenario() -> None:
        pump = asyncio.create_task(adapter._paced_sender.run())
        await adapter._close_after_goodbye()
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert log[0] == f"frames_sent:{int(8000 * 2.0) // 160}"


def test_marks_are_resolved_during_the_drain():
    """Marks arrive on the socket the receive loop owns. A drain that sleeps
    instead of reading leaves every one unresolved, so playback never reports
    finished and the wait always runs to its stall bound."""
    log: list[str] = []
    socket = FakeSocket()
    adapter = adapter_with_goodbye(1.0, socket, log)

    async def scenario() -> float:
        pump = asyncio.create_task(adapter._paced_sender.run())
        started = asyncio.get_running_loop().time()
        await adapter._drain_playback(service_socket=True)
        elapsed = asyncio.get_running_loop().time() - started
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass
        return elapsed

    elapsed = asyncio.run(scenario())
    assert not adapter._pending_marks, "every mark must have come back"
    # One second of audio. Anything near the 3s stall bound means the marks
    # were never read.
    assert elapsed < 2.0


# ---------------------------------------------------------------------------
# It still has to end
# ---------------------------------------------------------------------------
def test_a_customer_who_hangs_up_mid_goodbye_ends_the_wait_at_once():
    """Nobody is left to hear it, and marks can never come back -- holding
    the concurrency slot for the rest of the goodbye is pure waste."""
    log: list[str] = []
    socket = FakeSocket(echo_marks=False, stop_after=0.3)
    adapter = adapter_with_goodbye(20.0, socket, log)

    async def scenario() -> float:
        pump = asyncio.create_task(adapter._paced_sender.run())
        started = asyncio.get_running_loop().time()
        await adapter._drain_playback(service_socket=True)
        elapsed = asyncio.get_running_loop().time() - started
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass
        return elapsed

    elapsed = asyncio.run(scenario())
    assert elapsed < 2.0, "twenty seconds of audio, but nobody to play it to"
    assert adapter.audio_currently_playing is False


def test_a_silent_socket_cannot_hold_the_call_open_forever():
    """The hard cap is the only thing standing between a wedged stream and a
    permanently occupied concurrency slot."""
    log: list[str] = []
    socket = FakeSocket(echo_marks=False)
    adapter = adapter_with_goodbye(0.0, socket, log)
    adapter._pending_marks.add("never-acknowledged")

    async def scenario() -> float:
        started = asyncio.get_running_loop().time()
        await adapter._drain_playback(timeout=0.4, service_socket=True)
        return asyncio.get_running_loop().time() - started

    elapsed = asyncio.run(scenario())
    assert 0.4 <= elapsed < 2.0
    assert adapter.audio_currently_playing is True


def test_nothing_queued_means_no_added_delay():
    log: list[str] = []
    socket = FakeSocket()
    adapter = adapter_with_goodbye(0.0, socket, log)

    async def scenario() -> float:
        started = asyncio.get_running_loop().time()
        await adapter._drain_playback(service_socket=True)
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(scenario()) < 0.2


# ---------------------------------------------------------------------------
# The pump must not tear down the audio it is holding
# ---------------------------------------------------------------------------
def test_the_close_signal_does_not_discard_queued_agent_audio():
    """Breaking out of the outbound pump closed the paced sender, which threw
    the goodbye away before anyone could hear it. The pump now only raises a
    flag and keeps holding the audio."""
    goodbye = b"\xff" * 8000

    class Bridge:
        def __init__(self) -> None:
            self._messages = [("audio", goodbye), ("control", '{"event": "closing_call"}')]

        async def next_output(self):
            if self._messages:
                return self._messages.pop(0)
            await asyncio.sleep(3600)

    async def scenario() -> tuple[float, bool]:
        adapter = TwilioAdapter(audio_bridge=Bridge(), client=object())
        adapter.websocket = FakeSocket()
        adapter.stream_sid = "MZ"
        pump = asyncio.create_task(adapter._send_to_twilio())
        await asyncio.sleep(0.1)
        queued_or_playing = adapter.audio_currently_playing
        signalled = adapter._close_after_playback.is_set()
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass
        return queued_or_playing, signalled

    still_playing, signalled = asyncio.run(scenario())
    assert signalled is True, "the receive loop must be told to close"
    assert still_playing is True, "and the goodbye must survive until it does"
