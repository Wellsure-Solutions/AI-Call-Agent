from __future__ import annotations

"""Opt-in raw mu-law capture of both sides of a call.

Timing numbers alone cannot tell you whether an 800ms gap sounded like a
thoughtful pause or a dropped line, whether a "confirmed barge-in" cut the
agent off mid-word, or whether the caller energy that triggered it was the
customer or the agent's own voice coming back through a speakerphone. For
that you have to listen. This writes exactly what crossed the socket, in the
wire format, with no resampling.

Two files per call:
  * `<call_id>.in.ulaw`  -- caller audio as Twilio delivered it
  * `<call_id>.out.ulaw` -- agent audio as it was handed to Twilio

They are frame-aligned by construction: both are continuous 8 kHz mu-law, so
byte offset N in each file is the same instant on the line, and
`scripts/ulaw_to_wav.py` interleaves them into one stereo file for listening.

This is a debugging tool that records customer conversations. It writes
nothing unless `CALL_AGENT_MEDIA_DUMP_DIR` is set, and the captures must be
handled as call recordings: restricted directory, deliberate retention,
deleted when the measurement is finished.
"""

import logging
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

# mu-law silence. Outbound gaps are padded with it so the two files stay
# aligned to the same wall-clock instant even while only one side is talking.
_SILENCE_BYTE = 0xFF


class MediaDump:
    """Writes the inbound and outbound mu-law streams for a single call."""

    def __init__(self, directory: Path, call_id: str) -> None:
        self._directory = Path(directory)
        self._call_id = call_id
        self._inbound: BinaryIO | None = None
        self._outbound: BinaryIO | None = None
        self._inbound_bytes = 0
        self._outbound_bytes = 0

    @classmethod
    def create(cls, directory: Path | None, call_id: str) -> "MediaDump | None":
        if directory is None:
            return None
        dump = cls(directory, call_id)
        return dump if dump.open() else None

    def open(self) -> bool:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._inbound = (self._directory / f"{self._call_id}.in.ulaw").open("wb")
            self._outbound = (self._directory / f"{self._call_id}.out.ulaw").open("wb")
        except OSError:
            logger.warning("media_dump_open_failed", extra={"call_id": self._call_id})
            self.close()
            return False
        return True

    def write_inbound(self, frame: bytes) -> None:
        if self._inbound is None or not frame:
            return
        try:
            self._inbound.write(frame)
            self._inbound_bytes += len(frame)
        except OSError:
            self._inbound = None

    def write_outbound(self, frame: bytes) -> None:
        """Record an agent frame, padding to keep it aligned with inbound.

        The agent is silent for most of a call, and nothing is written to the
        socket during those stretches. Without padding, the outbound file
        would compress every silence away and the two streams would drift
        apart, which is precisely the alignment the capture exists to check.
        """
        if self._outbound is None or not frame:
            return
        try:
            # The frame is going out *now*, so it starts at the caller
            # stream's current position. Both sides then advance together and
            # alignment error stays inside one 20ms frame.
            gap = self._inbound_bytes - self._outbound_bytes
            if gap > 0:
                self._outbound.write(bytes([_SILENCE_BYTE]) * gap)
                self._outbound_bytes += gap
            self._outbound.write(frame)
            self._outbound_bytes += len(frame)
        except OSError:
            self._outbound = None

    def close(self) -> None:
        for handle in (self._inbound, self._outbound):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        self._inbound = None
        self._outbound = None
