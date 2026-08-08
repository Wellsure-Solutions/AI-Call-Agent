from __future__ import annotations

"""Synthetic 8 kHz mu-law fixtures for audio tests.

Generated rather than checked in as binaries so the acoustic conditions are
explicit and adjustable: a test that fails can be read to find out what
"noisy line" or "speakerphone echo" actually meant. Real captures from
`CALL_AGENT_MEDIA_DUMP_DIR` can be dropped into `tests/audio/` and used the
same way once a measured batch exists -- these fixtures exist so the
decision logic can be tested before any real call is placed.

Everything here produces bytes in exactly the format Twilio delivers and
accepts: 8 kHz, 1 byte per sample, G.711 mu-law, 160 bytes per 20ms frame.
"""

import math
import random

FRAME_BYTES = 160
FRAME_MS = 20
SAMPLE_RATE = 8000

_BIAS = 0x84
_CLIP = 32635


def linear_to_mulaw(sample: int) -> int:
    """Standard G.711 mu-law encode; the inverse of local_vad's decode table."""
    sign = 0x80 if sample < 0 else 0x00
    magnitude = min(abs(sample), _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not magnitude & mask:
        mask >>= 1
        exponent -= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def encode(samples: list[int]) -> bytes:
    return bytes(linear_to_mulaw(sample) for sample in samples)


def tone(amplitude: int, ms: int = FRAME_MS, frequency: float = 220.0, seed: int | None = None) -> bytes:
    """Voiced-like audio: a tone at a given peak amplitude.

    A pure tone has ~0.707x its peak as RMS, which is close enough to speech
    for threshold tests and, unlike noise, is exactly reproducible.
    """
    count = int(SAMPLE_RATE * ms / 1000)
    step = 2 * math.pi * frequency / SAMPLE_RATE
    return encode([int(amplitude * math.sin(step * index)) for index in range(count)])


def noise(amplitude: int, ms: int = FRAME_MS, seed: int = 1234) -> bytes:
    """Unvoiced line noise at a bounded amplitude."""
    generator = random.Random(seed)
    count = int(SAMPLE_RATE * ms / 1000)
    return encode([generator.randint(-amplitude, amplitude) for _ in range(count)])


def silence(ms: int = FRAME_MS) -> bytes:
    """True mu-law silence, as an idle Twilio stream delivers it."""
    return bytes([0xFF]) * int(SAMPLE_RATE * ms / 1000)


def frames(payload: bytes) -> list[bytes]:
    """Split a buffer into the 20ms frames the transport actually carries."""
    return [payload[index:index + FRAME_BYTES] for index in range(0, len(payload), FRAME_BYTES)]


# ---------------------------------------------------------------------------
# Named acoustic conditions, so tests read as scenarios rather than numbers.
# ---------------------------------------------------------------------------
def quiet_line(ms: int = 1000) -> list[bytes]:
    """A good line: noise floor well under the default 400 RMS threshold."""
    return frames(noise(60, ms))


def noisy_line(ms: int = 1000) -> list[bytes]:
    """A bad Indian PSTN line: noise floor above the default fixed threshold.

    This is the case the fixed 400 threshold gets wrong -- every frame reads
    as voiced, so a barge-in "confirms" on noise alone.
    """
    return frames(noise(900, ms))


def backchannel(ms: int = 300) -> list[bytes]:
    """A short "haan"/"ok": loud, but far shorter than BARGE_IN_CONFIRM_MS."""
    return frames(tone(6000, ms))


def genuine_interruption(ms: int = 1200) -> list[bytes]:
    """Sustained speech that should always stop the agent."""
    return frames(tone(8000, ms))


def agent_speech(ms: int = 400) -> list[bytes]:
    """Agent TTS output with the dynamics real speech has.

    Every other fixture here is a constant tone, which is fine for threshold
    tests but hides an entire class of bug in anything that summarises a
    *window* of agent audio: with a flat signal the window's peak and its
    mean are the same number, so a comparison against either behaves
    identically. Real TTS swings roughly 25 dB between a vowel peak and the
    gap after a word, and that gap is where the two diverge.

    The pattern is one short phrase: two stressed syllables, an unstressed
    one, and the pauses between them.
    """
    plan = ((9000, 120), (1200, 60), (7000, 100), (400, 60), (5000, 60))
    scaled = max(1, round(ms / sum(duration for _amplitude, duration in plan)))
    payload = b"".join(tone(amplitude, duration) for amplitude, duration in plan * scaled)
    return frames(payload)


def soft_interruption(ms: int = 1200) -> list[bytes]:
    """A customer who is genuinely speaking, but not loudly.

    Amplitude picked so the frames land at ~2570 RMS, which is the level a
    real call actually measured while the agent was mid-sentence -- the one
    barge-in in that batch, and it was misclassified as echo. Held here so
    the number in the regression test is the observed one rather than a
    round figure chosen to pass.
    """
    return frames(tone(3640, ms))


def speakerphone_echo(agent_amplitude: int = 9000, attenuation_db: float = 12.0, ms: int = 1000) -> list[bytes]:
    """The agent's own audio returning attenuated through a speakerphone.

    Held deliberately above the default 400 threshold, which is the trap:
    energy alone cannot separate this from the customer, and it only ever
    occurs while agent audio is playing.
    """
    amplitude = int(agent_amplitude / (10 ** (attenuation_db / 20)))
    return frames(tone(amplitude, ms, frequency=180.0))
