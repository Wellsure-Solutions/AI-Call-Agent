from __future__ import annotations

"""Lightweight, dependency-free "how loud, how long" check used to confirm a
genuine caller barge-in on Twilio calls.

Deliberately not a real VAD library and not a transcript classifier:
  * `audioop` (the stdlib mu-law codec) is deprecated and gone in Python
    3.13, so decoding is a small hand-rolled lookup table instead.
  * Duration, not content, is what decides a barge-in here. This function
    never looks at what Deepgram thinks was said, so Hindi/Hinglish
    spelling variance and STT garbling never enter the decision -- a
    half-second of sustained voice energy either happened or it didn't.

The separation this has to make -- backchannel versus real interruption --
turns out to be reachable from duration alone, but only once the hangover
is long enough to keep an utterance intact. Measured on a real recorded
call, run lengths are bimodal: backchannels land at or under 400ms and
genuine turns at or over 600ms, with little in between. A confirm
threshold sited in that gap separates them. What energy and duration still
cannot do is distinguish a short backchannel from a short objection
("no", "stop") -- both are brief, so the tuning fails toward letting the
agent be interrupted rather than talking over someone.
"""

from typing import Callable

# Standard ITU-T G.711 mu-law -> linear PCM16 decode. Sign convention isn't
# load-bearing here (RMS squares the samples), only magnitude is, so this
# just needs to be a faithful magnitude decode.
_BIAS = 0x84
_SIGN_BIT = 0x80
_QUANT_MASK = 0x0F
_SEG_SHIFT = 4
_SEG_MASK = 0x70


def _ulaw_byte_to_pcm16(u_byte: int) -> int:
    u_byte = ~u_byte & 0xFF
    magnitude = ((u_byte & _QUANT_MASK) << 3) + _BIAS
    magnitude <<= (u_byte & _SEG_MASK) >> _SEG_SHIFT
    magnitude -= _BIAS
    return -magnitude if (u_byte & _SIGN_BIT) else magnitude


# Built once at import time: a 256-entry lookup table is far cheaper per
# frame than recomputing the shift/mask chain for every byte of every 20ms
# frame over the life of a call.
_MULAW_DECODE_TABLE: tuple[int, ...] = tuple(_ulaw_byte_to_pcm16(i) for i in range(256))

# RMS only ever needs the square, and mu-law has just 256 distinct input
# bytes, so the multiply is precomputable too. Instrumentation now measures
# every inbound frame rather than only the frames during a barge-in pause --
# at 50 frames/sec/call the per-byte Python loop this replaces was the single
# hottest thing in the audio path.
_MULAW_SQUARE_TABLE: tuple[int, ...] = tuple(sample * sample for sample in _MULAW_DECODE_TABLE)


def decode_mulaw_to_pcm16(mulaw_frame: bytes) -> list[int]:
    """Decode an 8 kHz mu-law frame to linear PCM16 samples."""
    table = _MULAW_DECODE_TABLE
    return [table[b] for b in mulaw_frame]


def rms_energy(mulaw_frame: bytes) -> float:
    """RMS energy of a mu-law frame, computed directly in the decoded
    linear-PCM domain so the result is comparable to a normal loudness
    threshold."""
    if not mulaw_frame:
        return 0.0
    # Integer arithmetic throughout, so this is bit-for-bit identical to
    # summing table[b] * table[b] in a Python loop -- just without the loop.
    sum_sq = sum(map(_MULAW_SQUARE_TABLE.__getitem__, mulaw_frame))
    return (sum_sq / len(mulaw_frame)) ** 0.5


class AdaptiveNoiseFloor:
    """Tracks the line's noise floor and derives a voice threshold from it.

    A fixed threshold cannot work across Indian PSTN lines. Measured on a
    real recorded call, the noise floor sat at ~42 RMS while speech peaked
    around 1400-2900 -- so the old fixed 400 happened to land in the gap.
    On a poor line the floor rises well past 400 and every frame reads as
    voiced; on a very quiet line 400 sits far above a softly-spoken
    customer and no interruption is ever heard. Deriving the threshold from
    the floor tracks both.

    The floor is only updated while the agent is silent. During agent
    speech the inbound signal is the agent's own voice returning through
    the customer's handset or speakerphone, and folding that into the floor
    would make the threshold chase the echo.

    Speech is excluded from the estimate entirely rather than merely
    down-weighted. A slow rise rate is not enough: replayed against a real
    call, letting speech frames nudge the floor at even 0.002 per frame
    pushed it from 42 to 375 during one long utterance, taking the
    threshold to 2254 -- above the speech that was still in progress. The
    result is a floor that climbs whenever someone talks and goes deaf
    exactly when it matters.

    The estimator is asymmetric by design: fall toward observed minima
    quickly, rise by a fixed small proportion regardless of how loud the
    frame was. That keeps it able to follow a line that genuinely gets
    noisier without letting any single loud passage drag it upward.
    """

    def __init__(
        self,
        initial_floor: float = 60.0,
        multiplier: float = 6.0,
        minimum: float = 250.0,
        maximum: float = 4000.0,
        fall_rate: float = 0.10,
        rise_rate: float = 0.0006,
        acquire_frames: int = 25,
    ) -> None:
        self.floor = initial_floor
        self.multiplier = multiplier
        self.minimum = minimum
        self.maximum = maximum
        self.fall_rate = fall_rate
        self.rise_rate = rise_rate
        # 0.0006 per 20ms frame is ~3% per second: a line that genuinely got
        # noisier is tracked within a few seconds, while a burst of speech
        # moves the floor by a fraction of a percent.
        # 25 frames is the first 500ms of the stream, before anyone has
        # spoken -- long enough to characterise the line, short enough to be
        # over before the greeting finishes.
        self.acquire_frames = acquire_frames
        self.samples = 0

    def observe(self, energy: float, agent_playing: bool) -> None:
        if agent_playing:
            return
        self.samples += 1
        if self.samples <= self.acquire_frames:
            # Acquisition. The slow creep is right for tracking drift across
            # a call but far too slow to start from: a line that is already
            # noisy when it opens would spend its first ten seconds firing
            # false barge-ins while the estimate caught up. The line is
            # near-silent before anyone speaks, so a plain average over the
            # opening frames is both quick and accurate.
            self.floor += (energy - self.floor) / self.samples
            return
        if energy < self.floor:
            # Track observed minima quickly: a new quiet level is direct
            # evidence about the floor.
            self.floor += self.fall_rate * (energy - self.floor)
            return
        # Everything louder creeps the floor up by a fixed small proportion,
        # never toward the observed level. Letting loud frames pull the
        # estimate toward themselves ratchets: the threshold is a multiple of
        # the floor, so every rise widens the band of frames able to raise it
        # further, and within one call the floor ran away to the clamp. A
        # constant creep recovers from a genuinely noisier line in seconds
        # while remaining immune to how loud any individual frame was.
        self.floor *= 1.0 + self.rise_rate

    @property
    def threshold(self) -> float:
        """Voice threshold, clamped so neither extreme can disable barge-in.

        The floor alone is not a usable threshold -- speech has to clear it
        by a real margin or noise crosses constantly. The minimum keeps a
        pathologically quiet line from triggering on nothing; the maximum
        keeps a pathologically noisy one from raising the bar past normal
        speech and silently disabling interruption entirely.
        """
        return max(self.minimum, min(self.maximum, self.floor * self.multiplier))


class VoicedDurationTracker:
    """Tracks continuous voiced duration across successive fixed-size
    caller frames using RMS energy against a threshold.

    A short run of silence (a syllable gap, not the end of speech) doesn't
    immediately reset the running duration -- only a hangover of several
    consecutive silent frames does.

    The hangover length is load-bearing and was the cause of genuine
    interruptions being ignored. Replaying a real call through this tracker,
    a 3-frame (60ms) hangover shattered 25 actual speech segments into 94
    fragments, because ordinary gaps between syllables exceed 60ms. No
    fragment ever reached the confirm threshold, so a customer could talk
    over the agent indefinitely without being heard. At 10 frames (200ms)
    the same audio yields 26 runs -- one per real utterance -- and the run
    lengths separate cleanly into backchannels (<=400ms) and real turns
    (>=600ms), which is exactly the distinction the confirm threshold has
    to make.

    `threshold_provider`, when given, is consulted per frame so an adaptive
    floor can move during the call. `energy_threshold` remains supported
    for a fixed threshold.
    """

    def __init__(
        self,
        energy_threshold: float,
        frame_ms: int,
        hangover_frames: int = 10,
        threshold_provider: "Callable[[], float] | None" = None,
    ) -> None:
        self.energy_threshold = energy_threshold
        self.frame_ms = frame_ms
        self.hangover_frames = hangover_frames
        self.threshold_provider = threshold_provider
        self.voiced_ms = 0
        self.last_energy = 0.0
        self._silent_run = 0

    @property
    def active_threshold(self) -> float:
        if self.threshold_provider is not None:
            return self.threshold_provider()
        return self.energy_threshold

    def reset(self) -> None:
        self.voiced_ms = 0
        self._silent_run = 0

    def observe(self, mulaw_frame: bytes) -> bool:
        """Feed one caller frame. Returns whether this frame was voiced."""
        self.last_energy = rms_energy(mulaw_frame)
        voiced = self.last_energy >= self.active_threshold
        if voiced:
            self._silent_run = 0
            self.voiced_ms += self.frame_ms
        else:
            self._silent_run += 1
            if self._silent_run >= self.hangover_frames:
                self.voiced_ms = 0
        return voiced