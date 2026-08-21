from __future__ import annotations

"""G.711 mu-law <-> 16-bit linear PCM, as table lookups.

Exists because Exotel's bidirectional stream is 16-bit linear PCM and
everything above the socket in this repo is 8 kHz mu-law: the greeting and
closing caches are `.ulaw`, `PacedSender` measures its buffer in mu-law bytes,
the barge-in RMS path decodes mu-law, `conversation_engine` refuses to play
cached audio unless the profile says `mulaw`, and `scripts/ulaw_to_wav.py`
reads mu-law dumps. Transcoding at the one socket that needs it keeps all of
that true for both carriers; threading PCM up through the bridge would not.

Not `audioop`. That module is deprecated and **removed in Python 3.13**, and
`guide.md` supports 3.11+, so importing it would turn a working 3.11
deployment into an import-time crash on 3.13 -- taking down the whole app, not
just Exotel. `local_vad.py` already made this call for the decode direction
and says so in its own docstring; this is the same decision, applied to both
directions and shared.

Cost is a table lookup per sample: 160 samples per 20 ms frame, against an RMS
pass that already runs on every one of them.
"""

import array
import sys

from app.telephony.audio.local_vad import _MULAW_DECODE_TABLE

_BIAS = 0x84
_CLIP = 32635

_LITTLE_ENDIAN = sys.byteorder == "little"


def _linear_to_mulaw(sample: int) -> int:
    """Standard ITU-T G.711 mu-law encode.

    Mirrors `tests/fixtures.py::linear_to_mulaw`, which is an independent
    implementation of the same standard -- the two are cross-checked in the
    tests so a transposed table entry fails loudly rather than degrading audio
    by a few dB where nobody can pin it down.
    """
    sign = 0x80 if sample < 0 else 0x00
    magnitude = min(abs(sample), _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not magnitude & mask:
        mask >>= 1
        exponent -= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


# 65536 entries, built once at import (a few milliseconds). Indexed by the
# unsigned 16-bit reading of the sample, so no per-sample sign arithmetic is
# needed on the hot path.
_MULAW_ENCODE_TABLE: bytes = bytes(
    _linear_to_mulaw(value if value < 0x8000 else value - 0x10000) for value in range(0x10000)
)

# mu-law byte -> the two little-endian PCM16 bytes it decodes to. Lets the
# decode direction run as a single `bytes.translate`-style join rather than a
# Python loop over samples.
_MULAW_TO_PCM_BYTES: tuple[bytes, ...] = tuple(
    int(sample).to_bytes(2, "little", signed=True) for sample in _MULAW_DECODE_TABLE
)


def ulaw_to_pcm16(mulaw: bytes) -> bytes:
    """8 kHz mu-law -> 16-bit signed little-endian PCM. Doubles in length."""
    table = _MULAW_TO_PCM_BYTES
    return b"".join([table[byte] for byte in mulaw])


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    """16-bit signed little-endian PCM -> 8 kHz mu-law. Halves in length.

    A trailing odd byte is dropped rather than guessed at: half a sample is
    corrupt input, and inventing the other half puts a click on the line.
    """
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return b""
    samples = array.array("H")
    samples.frombytes(pcm[:usable])
    if not _LITTLE_ENDIAN:
        samples.byteswap()
    table = _MULAW_ENCODE_TABLE
    return bytes([table[value] for value in samples])
