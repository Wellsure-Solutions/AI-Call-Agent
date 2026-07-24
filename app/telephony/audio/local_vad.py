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

This module has exactly one shared design limitation, documented rather
than hidden: energy + duration alone cannot distinguish a short backchannel
("haan", "okay") from a short genuine objection ("no", "stop") -- both are
acoustically brief. See BARGE_IN_CONFIRM_MS in app.core.settings for how
that trade-off is tuned.
"""

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
    table = _MULAW_DECODE_TABLE
    sum_sq = 0
    for b in mulaw_frame:
        sample = table[b]
        sum_sq += sample * sample
    return (sum_sq / len(mulaw_frame)) ** 0.5


class VoicedDurationTracker:
    """Tracks continuous voiced duration across successive fixed-size
    caller frames using RMS energy against a fixed threshold.

    A short run of silence (a syllable gap, not the end of speech) doesn't
    immediately reset the running duration -- only a hangover of a few
    consecutive silent frames does. This keeps normal speech cadence from
    fragmenting into many short "voiced" runs that never individually
    cross the confirm threshold.
    """

    def __init__(self, energy_threshold: float, frame_ms: int, hangover_frames: int = 3) -> None:
        self.energy_threshold = energy_threshold
        self.frame_ms = frame_ms
        self.hangover_frames = hangover_frames
        self.voiced_ms = 0
        self._silent_run = 0

    def reset(self) -> None:
        self.voiced_ms = 0
        self._silent_run = 0

    def observe(self, mulaw_frame: bytes) -> bool:
        """Feed one caller frame. Returns whether this frame was voiced."""
        voiced = rms_energy(mulaw_frame) >= self.energy_threshold
        if voiced:
            self._silent_run = 0
            self.voiced_ms += self.frame_ms
        else:
            self._silent_run += 1
            if self._silent_run >= self.hangover_frames:
                self.voiced_ms = 0
        return voiced