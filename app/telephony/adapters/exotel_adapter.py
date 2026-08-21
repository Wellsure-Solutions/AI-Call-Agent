from __future__ import annotations

"""Exotel's media plane: the wire, and nothing else.

Every turn-taking decision is inherited from `StreamingMediaAdapter`
unchanged. What differs from Twilio is three things, all of them here:

  * **Encoding.** Exotel's bidirectional stream is 16-bit linear PCM, little
    endian, 8 kHz mono. It cannot be asked for mu-law -- the format is fixed
    on the inbound leg whatever it accepts outbound -- so this transcodes at
    the socket and everything above it still sees mu-law. That keeps the
    `.ulaw` greeting cache, the barge-in RMS maths, and
    `conversation_engine`'s `encoding != "mulaw"` guard true for both
    carriers.

  * **Framing.** Exotel requires chunks that are a multiple of 320 bytes,
    which at 8 kHz 16-bit is exactly one 20ms frame -- the same grid our
    160-byte mu-law frames sit on. Outbound frames are aggregated up to
    EXOTEL_SEND_CHUNK_BYTES before being sent; inbound chunks are split back
    down to 20ms, because every barge-in constant is expressed in 20ms frames
    and handing the tracker a 200ms frame would silently rescale all of them.

  * **Message names.** `stream_sid`, not Twilio's `streamSid`. Confirmed
    against Exotel's applet docs and Pipecat's shipped serializer.
"""

import base64
import json
import logging
import time

from app.core.settings import EXOTEL_SEND_CHUNK_BYTES
from app.telephony.adapters.streaming_media import StreamingMediaAdapter
from app.telephony.audio import g711
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.audio.local_vad import rms_energy
from app.telephony.audio.media_dump import MediaDump
from app.telephony.metrics import CallMetrics
from app.telephony.providers.exotel_provider import ExotelProvider

logger = logging.getLogger(__name__)

# One 20ms frame, in each representation. These are the same span of audio:
# 160 mu-law samples at one byte each, or 160 PCM16 samples at two.
MULAW_FRAME_BYTES = 160
PCM_FRAME_BYTES = 320
FRAME_SECONDS = 0.02

# Exotel's hard constraint. Everything we send is a whole number of these.
EXOTEL_CHUNK_ALIGNMENT = 320


def _aligned_chunk_bytes(requested: int) -> int:
    """Round the configured chunk size to something Exotel will accept.

    Rounded down to a multiple of 320, floored at one frame. A misconfigured
    value produces slightly different latency; an unaligned one produces the
    audio gaps and timing drift Exotel's docs warn about, which is far harder
    to diagnose from a recording.
    """
    aligned = (max(int(requested), EXOTEL_CHUNK_ALIGNMENT) // EXOTEL_CHUNK_ALIGNMENT) * EXOTEL_CHUNK_ALIGNMENT
    return max(aligned, EXOTEL_CHUNK_ALIGNMENT)


class ExotelAdapter(StreamingMediaAdapter):
    """Adapter for Exotel AgentStream bidirectional Voicebot streams."""

    def __init__(
        self,
        audio_bridge: AudioBridge | None = None,
        provider: ExotelProvider | None = None,
        metrics: CallMetrics | None = None,
        media_dump: MediaDump | None = None,
        send_chunk_bytes: int | None = None,
    ) -> None:
        super().__init__(audio_bridge=audio_bridge, metrics=metrics, media_dump=media_dump)
        self._provider = provider or ExotelProvider()
        self._send_chunk_bytes = _aligned_chunk_bytes(
            EXOTEL_SEND_CHUNK_BYTES if send_chunk_bytes is None else send_chunk_bytes
        )
        # Paced 20ms mu-law frames waiting to be batched into one wire chunk.
        self._outbound_pcm = bytearray()
        # Inbound audio re-framed to 20ms, with the arrival time of the chunk
        # each frame came from. Timestamps are carried rather than recomputed
        # so the latency metrics stay honest -- see `_split_inbound`.
        self._inbound_frames: list[tuple[bytes, float]] = []

    # ------------------------------------------------------------------
    # Outbound: mu-law frames in, aggregated PCM chunks out
    # ------------------------------------------------------------------
    async def send_audio(self, mulaw_frame: bytes) -> bool:
        """Accept one paced 20ms mu-law frame; flush when a chunk is full.

        PacedSender still runs on its own 20ms grid and is not modified -- its
        real-time anchor and `buffered_seconds` arithmetic are what the drain
        depends on. Batching happens after it, here, so the wire sees chunks
        of the size Exotel wants without pacing seeing anything different.
        """
        if self.websocket is None or self.stream_sid is None:
            logger.warning("send_audio called before Exotel WebSocket is bound; dropping frame")
            return False

        # Measured on the mu-law frame, before transcoding, so echo rejection
        # and the outbound metrics compare like with like against Twilio.
        self._recent_agent_rms.append(rms_energy(mulaw_frame))
        if self.metrics is not None:
            self.metrics.observe_outbound(mulaw_frame)
        if self.media_dump is not None:
            self.media_dump.write_outbound(mulaw_frame)

        self._outbound_pcm.extend(g711.ulaw_to_pcm16(mulaw_frame))
        if len(self._outbound_pcm) < self._send_chunk_bytes:
            return True
        return await self._flush_outbound()

    async def _flush_outbound(self) -> bool:
        """Send whole chunks; keep any remainder for the next frame."""
        while len(self._outbound_pcm) >= self._send_chunk_bytes:
            chunk = bytes(self._outbound_pcm[: self._send_chunk_bytes])
            del self._outbound_pcm[: self._send_chunk_bytes]
            if not await self._send_media(chunk):
                return False
            # One mark per wire chunk rather than per frame: a mark is only
            # meaningful at a boundary Exotel will actually acknowledge.
            await self._send_mark()
        return True

    async def _send_media(self, pcm_chunk: bytes) -> bool:
        sent = await self._send_json({
            "event": "media",
            "stream_sid": self.stream_sid,
            "media": {"payload": base64.b64encode(pcm_chunk).decode("ascii")},
        })
        if not sent:
            self.closing_requested = True
            logger.info(
                "Exotel stream no longer accepts outbound audio for call %s",
                self.session.call_id if self.session else "unknown",
            )
        return sent

    async def _on_frame_sent(self) -> None:
        # Marks are emitted by `_flush_outbound`, at chunk boundaries. The
        # base class's every-N-frames cadence would mark inside a chunk that
        # has not been sent yet, so the acknowledgement would say nothing
        # about what the customer has heard.
        return

    # ------------------------------------------------------------------
    # Inbound: PCM chunks in, 20ms mu-law frames out
    # ------------------------------------------------------------------
    async def receive_audio(self) -> bytes | None:
        """Yield one 20ms mu-law frame, reading from the socket as needed.

        Exotel chooses its own inbound chunk size and we do not control it. If
        those chunks reached the barge-in path directly, `hangover_frames=10`
        would stop meaning 200ms and every constant derived from real call
        audio would quietly mean something else. So chunks are split here and
        the rest of the pipeline never learns the difference.
        """
        assert self.websocket is not None
        while True:
            if self._inbound_frames:
                frame, arrived_at = self._inbound_frames.pop(0)
                return await self._observe_inbound_frame(frame, arrived_at)

            raw = await self.websocket.receive_text()
            # Stamped as close to arrival as we can, then carried through the
            # re-framer, so a chunk's audio is not all attributed to the
            # instant its last byte landed.
            arrived_at = time.monotonic()
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "media":
                payload = (msg.get("media") or {}).get("payload") or ""
                self._split_inbound(base64.b64decode(payload), arrived_at)
                continue

            if event == "stop":
                return None

            if event == "mark":
                # Exotel echoes a mark back once it has actually played the
                # audio preceding it -- the same ground truth Twilio's marks
                # give, and what `audio_currently_playing` trusts.
                name = (msg.get("mark") or {}).get("name")
                if name is not None:
                    self._pending_marks.discard(name)
                continue

            # "connected"/"start" (consumed by the route before start()) and
            # "dtmf" need no action here.

    def _split_inbound(self, pcm_chunk: bytes, arrived_at: float) -> None:
        """Split one PCM chunk into 20ms mu-law frames, timestamped correctly.

        The chunk's *last* sample is what arrived at `arrived_at`; everything
        before it is older by its offset from the end. Stamping every frame
        with `arrived_at` would place the caller's end-of-turn up to a whole
        chunk late, and `eot_to_first_audio_ms` -- measured from exactly that
        mark -- would read low by the same amount, so Twilio and Exotel could
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
            # Exotel promises multiples of 320 PCM bytes, so this should never
            # happen. Pad rather than drop: a short frame would desynchronise
            # the tracker's frame-count arithmetic for the rest of the call.
            tail = mulaw[total * MULAW_FRAME_BYTES:]
            padded = tail + bytes([0xFF]) * (MULAW_FRAME_BYTES - remainder)
            logger.warning("exotel_unaligned_chunk", extra={"bytes": len(pcm_chunk)})
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
        """Drop audio Exotel has buffered but not yet played."""
        if self.websocket is not None and self.stream_sid is not None:
            self._outbound_pcm.clear()
            await self._send_json({"event": "clear", "stream_sid": self.stream_sid})
            self._pending_marks.clear()

    async def _send_mark(self) -> None:
        if self.websocket is None or self.stream_sid is None:
            return
        self._next_mark_id += 1
        name = f"m{self._next_mark_id}"
        self._pending_marks.add(name)
        if not await self._send_json({
            "event": "mark",
            "stream_sid": self.stream_sid,
            "mark": {"name": name},
        }):
            self._pending_marks.discard(name)

    async def _request_provider_hangup(self) -> None:
        await self._provider.request_terminal(self.call_sid, "completed")

    async def connect(self) -> None:
        """Not used: Exotel calls are placed by ExotelProvider at dial time.

        Unlike Twilio there is no adapter-shaped dial path, because there is
        no TwiML step -- the stream URL has to be minted before the request is
        sent, so dialling belongs entirely to the provider.
        """
        raise NotImplementedError("Exotel calls are placed via ExotelProvider.dial()")
