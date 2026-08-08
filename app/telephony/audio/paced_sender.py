from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

SendFrame = Callable[[bytes], Awaitable[bool]]
FrameSentHook = Callable[[], Awaitable[None]]


class PacedSender:
    """Re-slices whatever chunk sizes Deepgram hands us into fixed 20ms
    (160-byte, 8 kHz mu-law) frames and drains them to Twilio at real-time
    rate, anchored to a monotonic clock.

    This is the foundation the rest of barge-in handling depends on:
    without pacing, Twilio's own internal buffer ends up holding audio our
    process has already handed off and lost control of, and pause/resume
    on our side becomes meaningless -- Twilio just keeps playing what it
    already has regardless of what we do next. Pacing keeps that buffer
    bounded, so pause/resume/discard here actually reflect what the caller
    is about to hear.

    How bounded is `lead_seconds`, which is a direct trade between audio
    continuity and how much a soft pause can still take back. At zero the
    buffer is about one frame and a pause stops essentially everything, but
    a single slow socket write starves Twilio and the customer hears a
    crackle. See the constructor.

    `time.sleep(0.02)` between frames would drift over a multi-minute call;
    instead every tick is computed from a fixed anchor plus an integer
    number of frame periods.
    """

    FRAME_BYTES = 160  # 8000 Hz * 20ms, 1 byte/sample mu-law
    FRAME_SECONDS = 0.02
    SILENCE_BYTE = 0xFF  # mu-law encoding of analog silence

    def __init__(
        self,
        send_frame: SendFrame,
        on_frame_sent: FrameSentHook | None = None,
        lead_seconds: float = 0.0,
    ) -> None:
        self._send_frame = send_frame
        self._on_frame_sent = on_frame_sent
        # How far ahead of real time frames may be delivered, i.e. how much
        # audio Twilio is allowed to hold. Pacing exactly to real time keeps
        # that buffer at roughly one frame, which is ideal for barge-in and
        # terrible for a jittery link: any delay writing to the socket leaves
        # Twilio with nothing to play, and the customer hears a gap or a
        # crackle. A small lead absorbs that jitter.
        #
        # The trade is real and bounded: a soft pause can no longer stop
        # audio Twilio already holds, so up to `lead_seconds` keeps playing
        # after a pause begins. A *confirmed* barge-in is unaffected -- it
        # sends `clear`, which empties Twilio's buffer immediately.
        self._lead_seconds = max(0.0, lead_seconds)
        self._buffer = bytearray()
        self._resumed = asyncio.Event()
        self._resumed.set()
        self._closed = False

    def feed(self, data: bytes) -> None:
        """Queue newly generated agent audio for pacing."""
        self._buffer.extend(data)

    @property
    def has_buffered_audio(self) -> bool:
        return len(self._buffer) > 0

    @property
    def buffered_seconds(self) -> float:
        """How long the queued audio will take to play, in real time.

        Exact rather than estimated: the buffer is 8 kHz mu-law at one byte
        per sample, and pacing emits it at real-time rate by construction. A
        caller deciding how long to wait for playback to finish should ask
        this instead of guessing a timeout -- a guess that is too small cuts
        the agent off mid-word.
        """
        return len(self._buffer) / (self.FRAME_BYTES / self.FRAME_SECONDS)

    def pause(self) -> None:
        """Stop emitting new frames. Whatever's already buffered here stays
        untouched -- this is what makes a false barge-in cheap: resuming
        later picks up exactly where we left off."""
        self._resumed.clear()

    def resume(self) -> None:
        """Continue emitting from exactly where pacing left off."""
        self._resumed.set()

    def discard(self) -> None:
        """A barge-in was confirmed: drop whatever hasn't been sent yet."""
        self._buffer.clear()

    def close(self) -> None:
        self._closed = True
        self._resumed.set()  # unblock run() so it can exit promptly

    async def run(self) -> None:
        """Drains buffered audio to the transport at real-time rate for the
        lifetime of the call. Intended to run as its own background task."""
        next_tick: float | None = None
        while not self._closed:
            await self._resumed.wait()
            if self._closed:
                return

            if not self._buffer:
                # Nothing queued right now -- don't busy-spin, and drop the
                # anchor so that when audio does arrive we start pacing
                # from "now" instead of bursting frames to catch up on
                # time that passed while the buffer was empty.
                await asyncio.sleep(self.FRAME_SECONDS)
                next_tick = None
                continue

            now = time.monotonic()
            if next_tick is None or (now - next_tick) > self.FRAME_SECONDS:
                # First frame, or we're resuming after a pause/empty-buffer
                # gap: re-anchor instead of trying to catch up.
                next_tick = now

            frame = bytes(self._buffer[: self.FRAME_BYTES])
            del self._buffer[: self.FRAME_BYTES]
            if len(frame) < self.FRAME_BYTES:
                frame = frame + bytes([self.SILENCE_BYTE]) * (self.FRAME_BYTES - len(frame))

            sent = await self._send_frame(frame)
            if not sent:
                return
            if self._on_frame_sent is not None:
                await self._on_frame_sent()

            next_tick += self.FRAME_SECONDS
            # Sleep only once we are `lead_seconds` ahead of the schedule, so
            # that much audio sits in Twilio's buffer as jitter headroom
            # instead of arriving just in time and occasionally late.
            delay = next_tick - self._lead_seconds - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)