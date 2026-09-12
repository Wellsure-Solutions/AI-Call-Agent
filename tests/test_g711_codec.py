from __future__ import annotations

"""G.711 correctness, checked against references rather than against itself.

A lin -> ulaw -> lin round-trip can be self-consistently wrong: a transposed
pair of table entries survives it perfectly, and the damage shows up as audio
that is slightly off in a way nobody can attribute. So the tables are pinned
against the ITU-T standard's own landmark values, against Python's `audioop`
where it still exists, and against `tests/fixtures.py`'s independent encoder.
"""

import pytest

from app.telephony.audio import g711
from app.telephony.audio.local_vad import rms_energy
from tests import fixtures


# ---------------------------------------------------------------------------
# Reference vectors from the G.711 standard itself
# ---------------------------------------------------------------------------
# A mu-law byte is stored inverted. Inverting it gives sign in bit 7 (1 =
# negative), exponent in bits 6-4, mantissa in bits 3-0, and the value is
#     (((mantissa << 3) + 0x84) << exponent) - 0x84
# carrying that sign. Each entry below is derived from that definition by
# hand, not copied from another codec -- the point is to be an independent
# check, so a table generated from the same buggy assumption cannot agree.
#
# 0xFF is analog silence: the byte PacedSender pads with and fixtures.silence()
# emits. Note that 0x00 is the most *negative* code, not the most positive --
# the inversion catches everyone once.
G711_REFERENCE = [
    # (mu-law byte, inverted, decoded PCM16 value)
    (0xFF, 0x00, 0),        # sign +, exp 0, mant 0  -> +0, analog silence
    (0x7F, 0x80, 0),        # sign -, exp 0, mant 0  -> -0, which is also 0
    (0x00, 0xFF, -32124),   # sign -, exp 7, mant 15 -> most negative
    (0x80, 0x7F, 32124),    # sign +, exp 7, mant 15 -> most positive
    (0xFE, 0x01, 8),        # sign +, exp 0, mant 1
    (0x7E, 0x81, -8),       # sign -, exp 0, mant 1
    (0xEF, 0x10, 132),      # sign +, exp 1, mant 0
    (0x6F, 0x90, -132),     # sign -, exp 1, mant 0
]


@pytest.mark.parametrize("code, inverted, expected", G711_REFERENCE)
def test_decode_matches_the_standards_landmark_values(code, inverted, expected):
    assert ~code & 0xFF == inverted, "the derivation above is self-consistent"
    decoded = g711.ulaw_to_pcm16(bytes([code]))
    assert int.from_bytes(decoded, "little", signed=True) == expected


def test_silence_decodes_to_zero_not_to_a_dc_offset():
    """0xFF is what an idle line delivers and what PacedSender pads with. A DC
    offset here would put a hum under every gap in the conversation."""
    assert g711.ulaw_to_pcm16(bytes([0xFF]) * 160) == b"\x00\x00" * 160


def test_encode_agrees_with_the_independent_fixture_implementation():
    """tests/fixtures.py has its own G.711 encoder, written from the standard
    for generating test audio. Two implementations agreeing across the whole
    input range is what catches a transposed table entry."""
    for sample in range(-32768, 32768, 7):  # coprime stride: hits every segment
        expected = fixtures.linear_to_mulaw(sample)
        actual = g711.pcm16_to_ulaw(int(sample).to_bytes(2, "little", signed=True))
        assert actual == bytes([expected]), f"disagreement at sample {sample}"


def test_decode_agrees_with_audioop_exactly_where_it_is_still_available():
    """Belt and braces on 3.11/3.12. Skipped on 3.13+, which is exactly why
    this codec does not use audioop in the first place."""
    audioop = pytest.importorskip("audioop", reason="removed in Python 3.13")
    mulaw = bytes(range(256))
    assert g711.ulaw_to_pcm16(mulaw) == audioop.ulaw2lin(mulaw, 2)


def test_encode_agrees_with_audioop_to_within_one_quantisation_step():
    """Not exact, deliberately, and the gap is understood.

    audioop quantises to 14 bits before encoding (the historical Sun
    implementation); this codec works on the full 16-bit sample so that it is
    the *exact* inverse of the decode table the barge-in path already uses --
    see the round-trip test below, which is the property that actually matters
    at a transcoding boundary.

    The two therefore pick an adjacent mu-law code on a small fraction of
    inputs, always at the segment boundaries. Bounding that here rather than
    skipping the comparison keeps the check able to catch a transposed table
    entry, which would show up as a wild disagreement rather than a
    neighbouring code.
    """
    audioop = pytest.importorskip("audioop", reason="removed in Python 3.13")
    pcm = b"".join(int(s).to_bytes(2, "little", signed=True) for s in range(-32768, 32768))
    ours, theirs = g711.pcm16_to_ulaw(pcm), audioop.lin2ulaw(pcm, 2)

    disagreements = [(a, b) for a, b in zip(ours, theirs) if a != b]
    assert len(disagreements) < len(pcm) // 2 * 0.01, "should differ on well under 1% of samples"
    for a, b in disagreements:
        decoded_a = int.from_bytes(audioop.ulaw2lin(bytes([a]), 2), "little", signed=True)
        decoded_b = int.from_bytes(audioop.ulaw2lin(bytes([b]), 2), "little", signed=True)
        # One step within a segment; at these levels that is well under 0.1 dB.
        assert abs(decoded_a - decoded_b) <= 1024


def test_the_decode_table_is_the_one_the_barge_in_path_already_uses():
    """Two decode tables would let the audio the customer hears and the RMS
    the barge-in decision is made on drift apart."""
    from app.telephony.audio.local_vad import _MULAW_DECODE_TABLE

    for code in range(256):
        decoded = int.from_bytes(g711.ulaw_to_pcm16(bytes([code])), "little", signed=True)
        assert decoded == _MULAW_DECODE_TABLE[code]


# ---------------------------------------------------------------------------
# Round-trip properties (necessary, not sufficient -- see the module docstring)
# ---------------------------------------------------------------------------
# G.711 has two codes for zero -- 0xFF is +0 and 0x7F is -0 -- and 16-bit
# two's complement has only one. So 0x7F is the single mu-law byte that cannot
# survive a round trip through PCM: it decodes to 0 and re-encodes to 0xFF, the
# other spelling of the same silence. audioop does exactly the same thing.
# This is the only exception, it is inaudible, and it is pinned below so that a
# second exception appearing is a test failure rather than a mystery.
NEGATIVE_ZERO = 0x7F


def test_mulaw_survives_a_round_trip_through_pcm_byte_for_byte():
    """Every mu-law byte except the redundant -0 decodes to a PCM value that
    re-encodes to itself, so audio crossing the Exotel boundary and back is
    unchanged."""
    original = bytes(code for code in range(256) if code != NEGATIVE_ZERO) * 4

    assert g711.pcm16_to_ulaw(g711.ulaw_to_pcm16(original)) == original


def test_negative_zero_is_the_only_byte_that_does_not_round_trip():
    every_code = bytes(range(256))
    returned = g711.pcm16_to_ulaw(g711.ulaw_to_pcm16(every_code))

    changed = {sent: got for sent, got in zip(every_code, returned) if sent != got}

    assert changed == {NEGATIVE_ZERO: 0xFF}
    # Both spellings mean silence, so nothing is audibly lost.
    assert g711.ulaw_to_pcm16(bytes([NEGATIVE_ZERO])) == g711.ulaw_to_pcm16(b"\xff")


def normalise_negative_zero(mulaw: bytes) -> bytes:
    return bytes(0xFF if byte == NEGATIVE_ZERO else byte for byte in mulaw)


def test_real_frames_round_trip_with_their_audio_bit_identical():
    """Real audio does contain -0, so the *bytes* are not identical -- one
    spelling of silence becomes the other. What must be identical, and is, is
    the audio those bytes decode to. That is the guarantee the transcoding
    boundary actually owes the rest of the pipeline.
    """
    frames = fixtures.genuine_interruption(200) + fixtures.quiet_line(200) + [fixtures.silence()]
    assert any(NEGATIVE_ZERO in frame for frame in frames), "this test is pointless without a -0"

    for frame in frames:
        pcm = g711.ulaw_to_pcm16(frame)
        returned = g711.pcm16_to_ulaw(pcm)

        assert g711.ulaw_to_pcm16(returned) == pcm, "the audio itself must be unchanged"
        assert returned == normalise_negative_zero(frame), "and only -0 may be respelled"


def test_round_trip_preserves_the_energy_the_barge_in_decision_uses():
    """If transcoding moved RMS, every tuned barge-in constant would mean
    something different on Exotel than on Twilio."""
    for frame in fixtures.agent_speech(400) + fixtures.soft_interruption(200):
        assert rms_energy(g711.pcm16_to_ulaw(g711.ulaw_to_pcm16(frame))) == rms_energy(frame)


# ---------------------------------------------------------------------------
# Sizes and malformed input
# ---------------------------------------------------------------------------
def test_lengths_are_exactly_doubled_and_halved():
    assert len(g711.ulaw_to_pcm16(b"\x00" * 160)) == 320
    assert len(g711.pcm16_to_ulaw(b"\x00" * 320)) == 160


def test_a_twenty_millisecond_mulaw_frame_is_exactly_one_exotel_frame():
    """160 mu-law bytes -> 320 PCM bytes. Exotel requires multiples of 320, so
    the two frame grids line up exactly and no repacking is ever needed."""
    assert len(g711.ulaw_to_pcm16(fixtures.silence(20))) == 320


def test_an_odd_trailing_byte_is_dropped_rather_than_guessed():
    """Half a sample is corrupt input; inventing the other half is a click."""
    assert g711.pcm16_to_ulaw(b"\x01\x02\x03") == g711.pcm16_to_ulaw(b"\x01\x02")


def test_empty_input_is_empty_output():
    assert g711.pcm16_to_ulaw(b"") == b""
    assert g711.ulaw_to_pcm16(b"") == b""
