#!/usr/bin/env python3
"""Turn a captured call's mu-law dumps into a stereo WAV you can listen to.

    python scripts/ulaw_to_wav.py dumps/<call_id>            # both sides
    python scripts/ulaw_to_wav.py dumps/<call_id>.in.ulaw    # one side

Left channel is the caller, right channel is the agent, so a barge-in that
the metrics recorded as "committed" can be checked by ear: did the agent
actually stop, and was the energy that stopped it the customer or the
agent's own voice returning through a speakerphone?

Written without `audioop`, which is gone in Python 3.13, using the same
mu-law decode table the VAD uses so what you hear matches what was measured.
"""

from __future__ import annotations

import argparse
import struct
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.telephony.audio.local_vad import _MULAW_DECODE_TABLE  # noqa: E402

SAMPLE_RATE = 8000
SILENCE_BYTE = 0xFF


def decode(path: Path) -> bytes:
    table = _MULAW_DECODE_TABLE
    raw = path.read_bytes()
    return struct.pack(f"<{len(raw)}h", *(table[b] for b in raw))


def write_wav(destination: Path, channels: list[bytes]) -> None:
    width = 2
    length = max((len(c) for c in channels), default=0)
    padded = [c + b"\x00" * (length - len(c)) for c in channels]
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(len(padded))
        handle.setsampwidth(width)
        handle.setframerate(SAMPLE_RATE)
        if len(padded) == 1:
            handle.writeframes(padded[0])
            return
        # Interleave sample-by-sample: both dumps are continuous 8 kHz mu-law
        # written from the same call, so sample N is the same instant in each.
        interleaved = bytearray(length * len(padded))
        for index, channel in enumerate(padded):
            for offset in range(0, length, width):
                position = offset * len(padded) + index * width
                interleaved[position:position + width] = channel[offset:offset + width]
        handle.writeframes(bytes(interleaved))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="a .ulaw file, or the dump prefix shared by .in.ulaw/.out.ulaw")
    parser.add_argument("-o", "--output", type=Path, default=None)
    args = parser.parse_args()

    inbound = Path(f"{args.source}.in.ulaw")
    outbound = Path(f"{args.source}.out.ulaw")
    if inbound.exists() or outbound.exists():
        channels = [decode(p) if p.exists() else b"" for p in (inbound, outbound)]
        destination = args.output or Path(f"{args.source}.wav")
    elif args.source.exists():
        channels = [decode(args.source)]
        destination = args.output or args.source.with_suffix(".wav")
    else:
        print(f"no dump found at {args.source}", file=sys.stderr)
        return 2

    write_wav(destination, channels)
    seconds = max(len(c) for c in channels) / 2 / SAMPLE_RATE
    print(f"wrote {destination} ({len(channels)}ch, {seconds:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
