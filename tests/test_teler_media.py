from __future__ import annotations

"""Teler's media plane: framing, the playout model, barge-in, and the drain.

Two of these groups exist because Teler is missing something the other two
carriers have.

`test_barge_in_acoustics.py` proves the soft barge-in decisions on Twilio, and
every one of those decisions depends on `audio_currently_playing` being true
while the agent is speaking -- that is what arms the echo filter. On Twilio and
Exotel that signal is a mark the carrier echoes back once it has actually
played the audio. **Teler acknowledges nothing.** So the acoustic tests are
re-run here against Teler's modelled playout instead, to prove the model arms
and disarms the filter at the same points a mark would.

`test_greeting_cache.py` proves the closing line survives, which depends on the
drain waiting for playback that has left this process. On Teler the same
question is asked of the model, plus one Teler-only failure the other carriers
cannot have: outbound audio is aggregated into 500ms chunks, so the tail of a
turn can be stranded in the aggregation buffer and never sent at all.
"""

import asyncio
import base64
import json
import time

import pytest

from app.core.settings import (
    BARGE_IN_ECHO_MARGIN,
    BARGE_IN_VOICE_ENERGY_THRESHOLD,
)
from app.telephony.adapters.teler_adapter import (
    MULAW_FRAME_BYTES,
    PCM_BYTES_PER_SECOND,
    PCM_FRAME_BYTES,
    TelerAdapter,
    _send_chunk_bytes,
)
from app.telephony.audio import g711
from app.telephony.audio.local_vad import rms_energy
from app.telephony.audio.paced_sender import PacedSender
from app.telephony.metrics import CallMetrics
from tests import fixtures


class FakeSocket:
    def __init__(self, inbound: list[str] | None = None) -> None:
        self.sent: list[dict] = []
        self._inbound = list(inbound or [])
        self.closed = False

    async def send_text(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def receive_text(self) -> str:
        if not self._inbound:
            raise AssertionError("inbound script exhausted")
        return self._inbound.pop(0)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    def of_type(self, kind: str) -> list[dict]:
        return [message for message in self.sent if message.get("type") == kind]


def audio_message(pcm: bytes, message_id: int = 1) -> str:
    return json.dumps({
        "type": "audio",
        "stream_id": "ms_1",
        "message_id": message_id,
        "data": {"audio_b64": base64.b64encode(pcm).decode()},
    })


def adapter(inbound: list[str] | None = None, send_chunk_ms: int = 500, metrics=None) -> TelerAdapter:
    built = TelerAdapter(audio_bridge=object(), metrics=metrics, send_chunk_ms=send_chunk_ms)
    built.websocket = FakeSocket(inbound)
    built.stream_sid = "ms_1"
    built.call_sid = "cs_1"
    return built


# ---------------------------------------------------------------------------
# Chunk sizing
# ---------------------------------------------------------------------------
def test_the_outbound_chunk_is_always_a_whole_number_of_twenty_ms_frames():
    """The aggregation buffer drains in whole frames. A chunk size that was not
    a multiple of one would leave a partial sample behind and shift every
    subsequent frame by a byte, which is a click on every chunk boundary for
    the rest of the call."""
    for requested in (1, 19, 20, 21, 500, 517, 2000):
        assert _send_chunk_bytes(requested) % PCM_FRAME_BYTES == 0
        assert _send_chunk_bytes(requested) >= PCM_FRAME_BYTES


def test_the_default_five_hundred_ms_is_frejuns_recommended_minimum():
    """FreJun ask for at least 500ms per chunk to avoid choppy playback. 500ms
    of 8kHz 16-bit mono is 8000 bytes."""
    assert _send_chunk_bytes(500) == 8000
    assert _send_chunk_bytes(500) / PCM_BYTES_PER_SECOND == pytest.approx(0.5)


def test_one_mulaw_frame_is_exactly_one_teler_pcm_frame():
    assert PCM_FRAME_BYTES == MULAW_FRAME_BYTES * 2 == 320


# ---------------------------------------------------------------------------
# Outbound framing
# ---------------------------------------------------------------------------
def test_outbound_audio_is_batched_up_to_the_chunk_size():
    teler = adapter(send_chunk_ms=500)

    async def scenario():
        # 25 frames is exactly one 500ms chunk, and the paced sender reports
        # more still queued so the tail rule does not fire early.
        teler._paced_sender = PacedSender(lambda _f: None)
        teler._paced_sender.feed(b"\xff" * 4000)
        for frame in fixtures.frames(fixtures.tone(6000, 500)):
            assert await teler.send_audio(frame)

    asyncio.run(scenario())

    media = teler.websocket.of_type("audio")
    assert len(media) == 1
    assert len(base64.b64decode(media[0]["audio_b64"])) == 8000


def test_the_tail_of_a_turn_is_flushed_rather_than_stranded():
    """The Teler-only failure this rule exists for.

    Exotel holds a partial chunk back until the next frame arrives. At Exotel's
    200ms that loses the last fifth of a second of a turn; at Teler's 500ms it
    would lose the last half-second of every goodbye -- audio the drain then
    dutifully waits for and which is never sent at all. When the paced sender
    has nothing further queued there is by definition nothing left to complete
    the chunk with.
    """
    teler = adapter(send_chunk_ms=500)
    frames = fixtures.frames(fixtures.tone(6000, 120))  # 6 x 20ms, well under a chunk

    async def scenario():
        teler._paced_sender = PacedSender(lambda _f: None)
        teler._paced_sender.feed(b"\xff" * 4000)  # the turn is still generating
        for frame in frames[:-1]:
            await teler.send_audio(frame)
        assert teler.websocket.of_type("audio") == [], (
            "while more audio is still queued, a partial chunk is held back"
        )
        teler._paced_sender.discard()  # the turn ends: nothing left to fill the chunk
        await teler.send_audio(frames[-1])

    asyncio.run(scenario())

    media = teler.websocket.of_type("audio")
    assert len(media) == 1, "the tail must still reach the customer"
    assert len(base64.b64decode(media[0]["audio_b64"])) == 6 * PCM_FRAME_BYTES, "120ms of PCM16"
    assert not teler._outbound_pcm, "nothing may be left behind"


def test_a_sender_that_cannot_keep_up_sends_small_chunks_rather_than_stalling():
    """The other side of the tail rule, stated so it is a decision and not an
    accident. When generation is running at or below real time the paced sender
    is empty on every frame, so every frame is its own chunk. Waiting to fill a
    500ms chunk in that state would not make playback smoother -- there is no
    audio to fill it with -- it would just add 500ms of silence to the gap.
    """
    teler = adapter(send_chunk_ms=500)
    teler._paced_sender = PacedSender(lambda _f: None)  # never has audio queued

    async def scenario():
        for frame in fixtures.frames(fixtures.tone(6000, 60)):
            await teler.send_audio(frame)

    asyncio.run(scenario())

    media = teler.websocket.of_type("audio")
    assert len(media) == 3, "each frame goes out as it arrives"
    assert all(len(base64.b64decode(m["audio_b64"])) == PCM_FRAME_BYTES for m in media)


def test_audio_is_transcoded_to_linear_pcm_on_the_way_out():
    """Everything above the socket is mu-law; Teler's wire is audio/l16."""
    teler = adapter(send_chunk_ms=20)
    frame = fixtures.tone(6000, 20)

    asyncio.run(teler.send_audio(frame))

    payload = base64.b64decode(teler.websocket.of_type("audio")[0]["audio_b64"])
    assert len(payload) == PCM_FRAME_BYTES
    assert payload == g711.ulaw_to_pcm16(frame)


# ---------------------------------------------------------------------------
# Inbound framing
# ---------------------------------------------------------------------------
def test_inbound_chunks_are_split_into_twenty_millisecond_mulaw_frames():
    """Every barge-in constant is expressed in 20ms frames. Handing the tracker
    a 400ms frame -- Teler's own default chunk_size -- would silently rescale
    all of them."""
    original = b"".join(fixtures.frames(fixtures.tone(6000, 400)))
    teler = adapter([audio_message(g711.ulaw_to_pcm16(original))])

    async def scenario():
        return [await teler.receive_audio() for _ in range(20)]

    received = asyncio.run(scenario())

    assert all(len(frame) == MULAW_FRAME_BYTES for frame in received)
    assert b"".join(received) == original, "audio must survive the split unchanged"


def test_frames_from_one_chunk_are_timestamped_by_their_offset_from_its_end():
    """The chunk's last sample is what arrived now; everything before it is
    older. Stamping them all identically would place the caller's end-of-turn a
    whole chunk late and make eot_to_first_audio_ms read low on Teler only --
    so the three carriers would stop being comparable on the same
    conversation."""
    original = b"".join(fixtures.frames(fixtures.tone(6000, 200)))  # 10 frames
    teler = adapter([audio_message(g711.ulaw_to_pcm16(original))])

    async def scenario():
        await teler.receive_audio()
        return list(teler._inbound_frames)

    remaining = asyncio.run(scenario())

    stamps = [stamp for _frame, stamp in remaining]
    assert stamps == sorted(stamps), "later frames must carry later timestamps"
    assert stamps[-1] - stamps[0] == pytest.approx(0.02 * 8, abs=1e-6)


def test_a_closed_socket_reads_as_end_of_stream_not_as_a_disconnection():
    """Teler documents no `stop` message -- the call ending is the socket
    closing. Letting the disconnect propagate would file every normal customer
    hangup as `ai_disconnected` instead of `completed`, which are opposite
    lifecycle states in every report downstream."""
    from fastapi import WebSocketDisconnect

    class ClosingSocket(FakeSocket):
        async def receive_text(self) -> str:
            raise WebSocketDisconnect(code=1000)

    teler = adapter()
    teler.websocket = ClosingSocket()

    assert asyncio.run(teler.receive_audio()) is None


# ---------------------------------------------------------------------------
# The playout model -- Teler's stand-in for marks
# ---------------------------------------------------------------------------
def test_audio_counts_as_playing_for_as_long_as_it_takes_to_play():
    """Teler acknowledges nothing, so `audio_currently_playing` would collapse
    to "the paced sender still holds bytes" -- false the instant the last frame
    is handed over, while up to a full chunk is still queued inside Teler."""
    teler = adapter(send_chunk_ms=20)
    teler._paced_sender = PacedSender(lambda _f: None)

    async def scenario():
        assert teler.audio_currently_playing is False
        await teler.send_audio(fixtures.tone(6000, 20))
        return teler.audio_currently_playing

    assert asyncio.run(scenario()) is True
    assert teler._playout_until > time.monotonic()


def test_the_model_expires_rather_than_claiming_audio_plays_forever():
    teler = adapter(send_chunk_ms=20)
    teler._paced_sender = PacedSender(lambda _f: None)

    async def scenario():
        await teler.send_audio(fixtures.tone(6000, 20))  # 20ms of audio
        await asyncio.sleep(0.1)
        return teler.audio_currently_playing

    assert asyncio.run(scenario()) is False


def test_back_to_back_chunks_accumulate_instead_of_resetting_the_deadline():
    """Each chunk must extend the deadline by its own duration, not move it to
    one chunk from now -- otherwise a long agent turn would report finished
    while most of it is still queued."""
    teler = adapter(send_chunk_ms=20)
    teler._paced_sender = PacedSender(lambda _f: None)

    async def scenario():
        started = time.monotonic()
        for frame in fixtures.frames(fixtures.tone(6000, 200)):  # 10 x 20ms
            await teler.send_audio(frame)
        return teler._playout_until - started

    assert asyncio.run(scenario()) == pytest.approx(0.2, abs=0.05)


def test_a_barge_in_clear_ends_the_modelled_playback_immediately():
    """Leaving the model running past a `clear` would hold the echo filter open
    against the customer who just interrupted -- the one moment it must not
    be."""
    teler = adapter(send_chunk_ms=20)
    teler._paced_sender = PacedSender(lambda _f: None)

    async def scenario():
        await teler.send_audio(fixtures.tone(6000, 20))
        assert teler.audio_currently_playing is True
        await teler.clear_playback()
        return teler.audio_currently_playing

    assert asyncio.run(scenario()) is False
    assert teler.websocket.of_type("clear")


# ---------------------------------------------------------------------------
# Barge-in acoustics, on Teler's modelled playback
# (mirrors test_barge_in_acoustics.py's adapter-level group)
# ---------------------------------------------------------------------------
class Bridge:
    def __init__(self) -> None:
        self.committed = False

    def commit_interruption(self) -> None:
        self.committed = True


def adapter_in_pause(
    agent_playing: bool = False, agent_frames: list[bytes] | None = None
) -> tuple[TelerAdapter, Bridge, CallMetrics]:
    metrics = CallMetrics("call-teler", voice_threshold=BARGE_IN_VOICE_ENERGY_THRESHOLD)
    metrics.bind()
    bridge = Bridge()
    built = TelerAdapter(audio_bridge=bridge, metrics=metrics)
    built.stream_sid = "ms_1"
    built._paced_sender = PacedSender(lambda _f: None)
    if agent_playing:
        # Where Twilio would add an unresolved mark, Teler advances its
        # modelled playout deadline. This is the substitution under test.
        built._extend_playout(PCM_BYTES_PER_SECOND)  # a second of agent audio
        played = agent_frames or [fixtures.tone(9000)] * 20
        for frame in played:
            built._recent_agent_rms.append(rms_energy(frame))
    built._begin_soft_pause()
    return built, bridge, metrics


def feed(built: TelerAdapter, frames: list[bytes]) -> None:
    for frame in frames:
        built._process_barge_in_signal(frame)


def test_a_sustained_interruption_stops_the_agent():
    built, bridge, _ = adapter_in_pause()
    feed(built, fixtures.genuine_interruption(1200))
    assert bridge.committed is True


def test_a_backchannel_does_not_stop_the_agent():
    built, bridge, _ = adapter_in_pause()
    feed(built, fixtures.backchannel(300) + fixtures.quiet_line(400))
    assert bridge.committed is False
    assert built._pause_started_at is None, "playback must have resumed"


def test_speakerphone_echo_does_not_interrupt_the_agent():
    """The echo filter is armed by `audio_currently_playing`. On Teler that is
    the modelled deadline, so this test is what proves the model is a working
    substitute for a mark rather than a comment."""
    built, bridge, _ = adapter_in_pause(agent_playing=True)
    feed(built, fixtures.speakerphone_echo(ms=2000))
    assert bridge.committed is False


def test_an_echo_only_pause_resumes_quickly_instead_of_timing_out():
    built, bridge, metrics = adapter_in_pause(agent_playing=True)
    feed(built, fixtures.speakerphone_echo(ms=2000))

    assert bridge.committed is False
    decisions = [payload for name, payload in metrics.drain() if name == "metrics_barge_in"]
    assert decisions, "the pause must have ended"
    assert decisions[0]["decision"] == "resume"
    assert decisions[0]["elapsed_ms"] < 1000, "and ended promptly, not on the timeout"


def test_a_real_customer_still_interrupts_through_playing_agent_audio():
    built, bridge, _ = adapter_in_pause(agent_playing=True)
    feed(built, fixtures.genuine_interruption(1500))
    assert bridge.committed is True


def test_a_quiet_customer_is_not_mistaken_for_echo_of_a_loud_syllable():
    agent = fixtures.agent_speech(400)
    built, bridge, _ = adapter_in_pause(agent_playing=True, agent_frames=agent)
    feed(built, fixtures.soft_interruption(1500))
    assert bridge.committed is True

    peak = max(rms_energy(frame) for frame in agent)
    caller = rms_energy(fixtures.soft_interruption(20)[0])
    assert caller < peak * BARGE_IN_ECHO_MARGIN, (
        "the fixture must still reproduce the old peak-referenced misclassification"
    )


def test_echo_rejection_is_inactive_when_no_agent_audio_is_playing():
    built, _, _ = adapter_in_pause(agent_playing=False)
    assert built._is_probable_echo(fixtures.quiet_line()[0]) is False


def test_a_caller_supplied_bridge_is_forced_into_soft_mode():
    """AudioBridge defaults to hard_interrupt=True and the route supplies its
    own bridge, so without this every Teler call would hard-cut and none of the
    soft barge-in above would ever execute in production."""
    from app.telephony.audio.audio_bridge import AudioBridge
    from app.telephony.call_session import CallSession

    session = CallSession(call_id="call-teler-soft", direction="teler")
    bridge = AudioBridge(session)
    assert bridge.hard_interrupt is True

    TelerAdapter(audio_bridge=bridge).attach(session)

    assert bridge.hard_interrupt is False


# ---------------------------------------------------------------------------
# Letting the closing line finish
# (mirrors test_greeting_cache.py's drain group)
# ---------------------------------------------------------------------------
def test_the_greeting_is_queued_before_the_sender_task_starts():
    """The whole point of the cache: the first frame leaves on the next tick
    rather than after a websocket handshake and speech synthesis."""
    greeting = b"".join(fixtures.frames(fixtures.tone(8000, ms=1000)))

    class SilentBridge:
        async def next_output(self):
            await asyncio.sleep(3600)

    async def scenario() -> int:
        built = adapter(send_chunk_ms=20)
        built.audio_bridge = SilentBridge()
        built.pending_greeting = greeting
        task = asyncio.create_task(built._run_outbound_pump())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return len(built.websocket.of_type("audio"))

    assert asyncio.run(scenario()) > 0, "greeting audio must start flowing immediately"


def test_no_greeting_configured_changes_nothing():
    class SilentBridge:
        async def next_output(self):
            await asyncio.sleep(3600)

    async def scenario() -> int:
        built = adapter(send_chunk_ms=20)
        built.audio_bridge = SilentBridge()
        task = asyncio.create_task(built._run_outbound_pump())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return len(built.websocket.sent)

    assert asyncio.run(scenario()) == 0


def test_the_drain_waits_for_audio_already_handed_to_teler():
    """The bug the drain exists for, asked of the model rather than of a mark.

    Without the playout model this returns immediately: the paced sender is
    empty the instant the last frame is handed over, so the goodbye would be
    cut off by however much Teler still had queued.
    """
    built = adapter(send_chunk_ms=20)
    built._paced_sender = PacedSender(lambda _f: None)

    async def scenario() -> float:
        await built.send_audio(fixtures.tone(6000, 20))
        # Half a second of audio handed over, with nothing left in the sender.
        built._extend_playout(PCM_BYTES_PER_SECOND // 2)
        assert built.buffered_playback_seconds == 0.0, "the sender itself is empty"
        started = time.monotonic()
        await built._drain_playback(timeout=5.0)
        return time.monotonic() - started

    waited = asyncio.run(scenario())

    assert waited >= 0.4, f"the drain returned after {waited:.2f}s, cutting the goodbye off"
    assert built.audio_currently_playing is False


def test_the_drain_still_terminates_when_nothing_is_playing():
    built = adapter()
    built._paced_sender = PacedSender(lambda _f: None)

    assert built.audio_currently_playing is False
    asyncio.run(built._drain_playback(timeout=5.0))
