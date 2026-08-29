from __future__ import annotations

"""Teler's media plane: the wire, and nothing else.

Every turn-taking decision is inherited from `StreamingMediaAdapter`
unchanged. What differs from Twilio and Exotel is four things, all of them
here:

  * **Encoding.** Teler's stream is 16-bit linear PCM, little endian, 8 kHz
    mono -- its own `start` message says so: `{"encoding": "audio/l16",
    "sample_rate": 8000, "channels": 1}`. Identical to Exotel, so the same
    transcode happens at the socket and everything above it still sees mu-law.
    That keeps the `.ulaw` greeting cache, the barge-in RMS maths and
    `conversation_engine`'s `encoding != "mulaw"` guard true for all three
    carriers.

  * **Message names.** Teler says `{"type": "audio"}` where Twilio says
    `{"event": "media"}`, and the payload nesting is *asymmetric*: inbound
    audio arrives at `msg["data"]["audio_b64"]`, outbound audio is sent at the
    top level as `audio_b64` with a `chunk_id`. Both of FreJun's reference
    bridges and their own SDK README agree on the asymmetry; it is not a typo,
    and it is pinned in tests/test_teler_wire_format.py.

  * **No marks.** This is the big one. Teler has no acknowledgement of any
    kind -- its only playback controls are `clear` (wipe the queue) and
    `interrupt` (drop one chunk_id), and both are commands travelling
    outbound. Twilio and Exotel both echo a mark back once the audio before it
    has actually played, and `audio_currently_playing` is built on that ground
    truth. Without it, "playing" would collapse to "the paced sender still
    holds bytes", which goes false the instant the last frame is handed to the
    socket -- while up to a full chunk of the goodbye is still queued inside
    Teler. The drain would end there and cut the closing line off mid-word,
    which is the exact bug `_close_after_goodbye` exists to prevent.

    So playback is *modelled* instead: every chunk sent advances a real-time
    playout deadline by its own duration, and `audio_currently_playing` stays
    true until that deadline passes. It is a model, not evidence -- it cannot
    see Teler's own jitter buffer, and it will read "finished" slightly early
    on a congested socket. It is strictly better than nothing, and it is the
    best available until FreJun ship an acknowledgement.

  * **No stop event.** FreJun document exactly two inbound messages, `start`
    and `audio`; the call ending is the socket closing. So the disconnect is
    caught here and reported as end-of-stream, so that a customer hanging up
    on Teler produces the same `completed` outcome it produces on Twilio
    rather than an `ai_disconnected` failure.

Framing needs no re-alignment work of Exotel's kind: Teler's `chunk_size` is
in milliseconds and must be a multiple of 20, so inbound chunks always split
into whole 20ms frames. The splitter is still written for the general case,
because `chunk_size` is configurable and a partial frame must never reach the
barge-in tracker.
"""

import base64
import json
import logging
import time

from fastapi import WebSocketDisconnect

from app.core.settings import TELER_SEND_CHUNK_MS
from app.telephony.adapters.streaming_media import StreamingMediaAdapter
from app.telephony.audio import g711
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.audio.local_vad import rms_energy
from app.telephony.audio.media_dump import MediaDump
from app.telephony.metrics import CallMetrics
from app.telephony.providers.teler_provider import TelerProvider

logger = logging.getLogger(__name__)

# One 20ms frame, in each representation. These are the same span of audio:
# 160 mu-law samples at one byte each, or 160 PCM16 samples at two.
MULAW_FRAME_BYTES = 160
PCM_FRAME_BYTES = 320
FRAME_SECONDS = 0.02

# 8 kHz, 16-bit, mono: what one second of Teler's wire audio weighs. Used to
# turn bytes sent into the seconds of playback they represent.
PCM_BYTES_PER_SECOND = 8000 * 2

# ---------------------------------------------------------------------------
# Message names, pinned as constants and asserted byte-for-byte in
# tests/test_teler_wire_format.py. Exotel's casing incident is the precedent:
# a silent drift here breaks every call at once and looks like a credentials
# problem.
# ---------------------------------------------------------------------------
TYPE_KEY = "type"
TYPE_AUDIO = "audio"
TYPE_CLEAR = "clear"

# Inbound audio is nested under `data`; outbound is not. Asymmetric on purpose.
INBOUND_DATA_KEY = "data"
AUDIO_B64_KEY = "audio_b64"
CHUNK_ID_KEY = "chunk_id"

# Teler's fourth message, `{"type": "interrupt", "chunk_id": N}`, cancels one
# queued chunk. It has no constant here because it is deliberately not sent:
# after a confirmed barge-in nothing already queued should still play, which is
# what `clear` does in one message. Named so the omission reads as a decision.


def _send_chunk_bytes(chunk_ms: int) -> int:
    """PCM bytes per outbound message, from a millisecond setting.

    Rounded down to a whole 20ms frame and floored at one, so the outbound
    buffer always drains into whole frames and the mu-law frame grid the rest
    of the pipeline runs on is never broken by a partial sample.
    """
    frames = max(1, int(chunk_ms) // 20)
    return frames * PCM_FRAME_BYTES


class TelerAdapter(StreamingMediaAdapter):
    """Adapter for FreJun Teler bidirectional media streams."""

    def __init__(
        self,
        audio_bridge: AudioBridge | None = None,
        provider: TelerProvider | None = None,
        metrics: CallMetrics | None = None,
        media_dump: MediaDump | None = None,
        send_chunk_ms: int | None = None,
    ) -> None:
        super().__init__(audio_bridge=audio_bridge, metrics=metrics, media_dump=media_dump)
        self._provider = provider or TelerProvider()
        self._send_chunk_bytes = _send_chunk_bytes(
            TELER_SEND_CHUNK_MS if send_chunk_ms is None else send_chunk_ms
        )
        # Paced 20ms mu-law frames, transcoded, waiting to be batched into one
        # wire chunk.
        self._outbound_pcm = bytearray()
        # Inbound audio re-framed to 20ms, with the arrival time of the chunk
        # each frame came from. Timestamps are carried rather than recomputed
        # so the latency metrics stay honest -- see `_split_inbound`.
        self._inbound_frames: list[tuple[bytes, float]] = []
        # Teler's `chunk_id`, which identifies a queued chunk well enough to
        # `interrupt` one individually. Barge-in uses `clear` instead, but the
        # id is required on every audio message.
        self._next_chunk_id = 0
        # Monotonic deadline at which the last byte handed to Teler will have
        # finished playing. Stands in for the mark acknowledgements Teler does
        # not send; see the module docstring.
        self._playout_until = 0.0

    # ------------------------------------------------------------------
    # Playback state, modelled rather than acknowledged
    # ------------------------------------------------------------------
    @property
    def audio_currently_playing(self) -> bool:
        """True while agent audio is queued here or (modelled) inside Teler.

        The base class reads unresolved marks plus the paced sender's buffer.
        Teler acknowledges nothing, so the mark half is always empty and the
        sender half goes false a whole chunk early. The modelled deadline
        replaces what the marks used to supply.
        """
        return super().audio_currently_playing or time.monotonic() < self._playout_until

    def _extend_playout(self, pcm_bytes: int) -> None:
        """Advance the playout deadline by the duration of what was just sent.

        Anchored at `max(now, deadline)` so that a gap in generation does not
        let the model claim audio is still playing from a burst that finished
        long ago, and so that back-to-back chunks accumulate rather than each
        resetting the deadline to one chunk from now.
        """
        now = time.monotonic()
        self._playout_until = max(self._playout_until, now) + pcm_bytes / PCM_BYTES_PER_SECOND

    # ------------------------------------------------------------------
    # Outbound: mu-law frames in, aggregated PCM chunks out
    # ------------------------------------------------------------------
    async def send_audio(self, mulaw_frame: bytes) -> bool:
        """Accept one paced 20ms mu-law frame; flush when a chunk is ready.

        PacedSender still runs on its own 20ms grid and is not modified -- its
        real-time anchor and `buffered_seconds` arithmetic are what the drain
        depends on. Batching happens after it, here, so the wire sees the
        larger chunks FreJun ask for without pacing seeing anything different.
        """
        if self.websocket is None or self.stream_sid is None:
            logger.warning("send_audio called before Teler WebSocket is bound; dropping frame")
            return False

        # Measured on the mu-law frame, before transcoding, so echo rejection
        # and the outbound metrics compare like with like against Twilio.
        self._recent_agent_rms.append(rms_energy(mulaw_frame))
        if self.metrics is not None:
            self.metrics.observe_outbound(mulaw_frame)
        if self.media_dump is not None:
            self.media_dump.write_outbound(mulaw_frame)

        self._outbound_pcm.extend(g711.ulaw_to_pcm16(mulaw_frame))
        return await self._flush_outbound()

    async def _flush_outbound(self) -> bool:
        """Send whole chunks, and the tail when there is nothing left to fill it.

        The tail rule is not an optimisation, it is required for correctness.
        Sending only whole chunks would strand up to `_send_chunk_bytes` of
        audio in this buffer for as long as no further frame arrived -- and at
        the 500ms default that is the last half-second of every agent turn,
        including the goodbye the drain exists to protect. When the paced
        sender has nothing further queued there is by definition nothing left
        to complete the chunk with, so holding it back only creates the gap it
        was trying to avoid.
        """
        while len(self._outbound_pcm) >= self._send_chunk_bytes:
            chunk = bytes(self._outbound_pcm[: self._send_chunk_bytes])
            del self._outbound_pcm[: self._send_chunk_bytes]
            if not await self._send_media(chunk):
                return False
        if self._outbound_pcm and not self._sender_has_more():
            tail = bytes(self._outbound_pcm)
            self._outbound_pcm.clear()
            return await self._send_media(tail)
        return True

    def _sender_has_more(self) -> bool:
        """Whether the paced sender still holds audio behind the current frame.

        `send_audio` runs from inside PacedSender.run(), after the frame it is
        being given has already been removed from that buffer -- so an empty
        buffer here means this frame was the last one queued.
        """
        return self._paced_sender is not None and self._paced_sender.has_buffered_audio

    async def _send_media(self, pcm_chunk: bytes) -> bool:
        self._next_chunk_id += 1
        sent = await self._send_json({
            TYPE_KEY: TYPE_AUDIO,
            AUDIO_B64_KEY: base64.b64encode(pcm_chunk).decode("ascii"),
            CHUNK_ID_KEY: self._next_chunk_id,
        })
        if not sent:
            self.closing_requested = True
            logger.info(
                "Teler stream no longer accepts outbound audio for call %s",
                self.session.call_id if self.session else "unknown",
            )
            return False
        self._extend_playout(len(pcm_chunk))
        return True

    async def _on_frame_sent(self) -> None:
        # The base class marks every 5 frames. Teler has nothing to mark, and
        # the playout model is advanced per wire chunk in `_send_media`, so
        # there is nothing to do per frame.
        return

    # ------------------------------------------------------------------
    # Inbound: PCM chunks in, 20ms mu-law frames out
    # ------------------------------------------------------------------
    async def receive_audio(self) -> bytes | None:
        """Yield one 20ms mu-law frame, reading from the socket as needed.

        Teler's inbound chunk size is whatever `chunk_size` we asked for in the
        stream flow, and it is configurable. If those chunks reached the
        barge-in path directly, `hangover_frames=10` would stop meaning 200ms
        and every constant derived from real call audio would quietly mean
        something else. So chunks are split here and the rest of the pipeline
        never learns the difference.

        A closed socket returns None rather than propagating. Teler documents
        no `stop` message -- "Teler closes the WebSocket connection when the
        call ends" -- so the close *is* the stop event, and reporting it as a
        disconnection instead would file every normal customer hangup as
        `ai_disconnected` rather than `completed`.
        """
        assert self.websocket is not None
        while True:
            if self._inbound_frames:
                frame, arrived_at = self._inbound_frames.pop(0)
                return await self._observe_inbound_frame(frame, arrived_at)

            try:
                raw = await self.websocket.receive_text()
            except WebSocketDisconnect:
                return None
            # Stamped as close to arrival as we can, then carried through the
            # re-framer, so a chunk's audio is not all attributed to the
            # instant its last byte landed.
            arrived_at = time.monotonic()
            msg = json.loads(raw)

            if msg.get(TYPE_KEY) == TYPE_AUDIO:
                payload = (msg.get(INBOUND_DATA_KEY) or {}).get(AUDIO_B64_KEY) or ""
                self._split_inbound(base64.b64decode(payload), arrived_at)
                continue

            # `start` is consumed by the route before start() is called. Teler
            # documents no other inbound message; anything new is ignored
            # rather than guessed at.

    def _split_inbound(self, pcm_chunk: bytes, arrived_at: float) -> None:
        """Split one PCM chunk into 20ms mu-law frames, timestamped correctly.

        The chunk's *last* sample is what arrived at `arrived_at`; everything
        before it is older by its offset from the end. Stamping every frame
        with `arrived_at` would place the caller's end-of-turn up to a whole
        chunk late, and `eot_to_first_audio_ms` -- measured from exactly that
        mark -- would read low by the same amount, so the three carriers could
        not be compared on the same conversation.
        """
        mulaw = g711.pcm16_to_ulaw(pcm_chunk)
        total = len(mulaw) // MULAW_FRAME_BYTES
        for index in range(total):
            frame = mulaw[index * MULAW_FRAME_BYTES:(index + 1) * MULAW_FRAME_BYTES]
            age = (total - 1 - index) * FRAME_SECONDS
            self._inbound_frames.append((frame, arrived_at - age))
        remainder = len(mulaw) % MULAW_FRAME_BYTES
        if remainder:
            # Teler's chunk_size is a multiple of 20ms, so this should never
            # happen. Pad rather than drop: a short frame would desynchronise
            # the tracker's frame-count arithmetic for the rest of the call.
            tail = mulaw[total * MULAW_FRAME_BYTES:]
            padded = tail + bytes([0xFF]) * (MULAW_FRAME_BYTES - remainder)
            logger.warning("teler_unaligned_chunk", extra={"bytes": len(pcm_chunk)})
            self._inbound_frames.append((padded, arrived_at))

    async def _observe_inbound_frame(self, frame: bytes, arrived_at: float) -> bytes:
        """Per-frame bookkeeping, in the same order and cadence as Twilio's."""
        playing = self.audio_currently_playing
        energy = rms_energy(frame)
        # The floor must be learned continuously, not only while a pause is
        # active -- by the time a barge-in candidate opens, the threshold has
        # to already be right.
        self._noise_floor.observe(energy, playing)
        # Reuses the energy already computed above rather than a second pass
        # over the frame; this runs on every inbound frame for the whole call.
        await self._observe_idle(energy >= self._noise_floor.threshold, playing)
        if self.metrics is not None:
            self.metrics.observe_inbound(frame, at=arrived_at)
        if self.media_dump is not None:
            self.media_dump.write_inbound(frame)
        return frame

    # ------------------------------------------------------------------
    # Control messages
    # ------------------------------------------------------------------
    async def clear_playback(self) -> None:
        """Drop audio Teler has queued but not yet played.

        `clear` wipes Teler's whole chunk queue in one message, which is what a
        confirmed barge-in wants. `interrupt` exists for cancelling a single
        chunk_id and is deliberately not used: after a barge-in nothing already
        queued should still be played.

        The playout model is reset with it. Leaving it running would keep
        `audio_currently_playing` true for audio that has just been discarded,
        which would hold the echo filter open against the customer who was
        speaking -- the one moment it must not be.
        """
        if self.websocket is not None and self.stream_sid is not None:
            self._outbound_pcm.clear()
            self._playout_until = 0.0
            await self._send_json({TYPE_KEY: TYPE_CLEAR})
            self._pending_marks.clear()

    async def _send_mark(self) -> None:
        """No-op: Teler has no mark, and nothing to acknowledge one.

        Kept as an explicit override rather than left to the base class's
        `NotImplementedError`, because the alternative is a crash halfway
        through the first agent turn. Playback progress is modelled in
        `_extend_playout` instead; see the module docstring.
        """
        return

    async def _request_provider_hangup(self) -> None:
        await self._provider.request_terminal(self.call_sid, "completed")

    async def connect(self) -> None:
        """Not used: Teler calls are placed by TelerProvider at dial time.

        Twilio keeps an adapter-shaped dial path because its own adapter
        predates the provider split. Teler never had one, so there is nothing
        to preserve, and the control plane stays entirely on the provider.
        """
        raise NotImplementedError("Teler calls are placed via TelerProvider.dial()")
