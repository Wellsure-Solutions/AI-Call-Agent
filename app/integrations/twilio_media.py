from __future__ import annotations

import base64


def encode_media_payload(mulaw_frame: bytes) -> str:
    """Encode an 8 kHz mu-law frame for a Twilio Media Streams message."""

    return base64.b64encode(mulaw_frame).decode("ascii")


def decode_media_payload(payload: str) -> bytes:
    """Decode a Twilio Media Streams payload without changing its audio."""

    return base64.b64decode(payload)