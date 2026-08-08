from __future__ import annotations

"""Pre-rendered greeting audio, played the instant the media stream binds.

Measured on a real answered call, the first agent audio arrived 2.0 seconds
after the stream bound. None of that is thinking time -- it is a Deepgram
websocket handshake, a settings round-trip, and greeting synthesis, all
happening *after* the customer has already said "hello". Add Twilio's own
answer-to-stream setup on top and the caller waits 4-5 seconds before
hearing anything, which reads as a dead line.

The greeting is the one utterance in the call that is known in advance, so
none of that work needs to be on the critical path. Rendering it once and
replaying the bytes turns a 2-second wait into the time it takes to write a
frame to a socket, while Deepgram connects in parallel behind it.

The cache key covers every input that changes how the greeting sounds, so
changing the text, the voice, the model, or the language silently
invalidates it rather than playing a stale line in the wrong voice. A miss
is never fatal: the caller falls back to Deepgram synthesising the greeting
exactly as before.
"""

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 8 kHz mu-law, 1 byte per sample. A greeting far longer than this is a
# script problem, and refusing to cache it keeps a corrupt or truncated file
# from being played to customers.
_MAX_GREETING_SECONDS = 30
_MAX_BYTES = 8000 * _MAX_GREETING_SECONDS
_MIN_BYTES = 8000 // 4  # 250ms; anything shorter is a failed render


def greeting_fingerprint(text: str, provider: str, model_id: str, voice_id: str, language: str) -> str:
    """Stable key over everything that affects how the greeting sounds."""
    material = "\x1f".join((text, provider, model_id, voice_id, language))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def cache_path(directory: Path, fingerprint: str) -> Path:
    return Path(directory) / f"greeting-{fingerprint}.ulaw"


def load_greeting(directory: Path | None, fingerprint: str) -> bytes | None:
    """Return cached mu-law greeting audio, or None to fall back to the provider."""
    if directory is None:
        return None
    path = cache_path(directory, fingerprint)
    try:
        audio = path.read_bytes()
    except OSError:
        return None
    if not _MIN_BYTES <= len(audio) <= _MAX_BYTES:
        logger.warning(
            "greeting_cache_rejected",
            extra={"bytes": len(audio), "fingerprint": fingerprint},
        )
        return None
    return audio


def save_greeting(directory: Path, fingerprint: str, audio: bytes) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = cache_path(directory, fingerprint)
    path.write_bytes(audio)
    return path
