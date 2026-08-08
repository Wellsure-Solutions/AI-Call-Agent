from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class AnswerField:
    """A temporary answer field collected from the active prompt."""

    name: str
    question: str
    allowed_values: tuple[str, ...] = ("yes", "no", "unknown")


@dataclass
class TranscriptTurn:
    role: str
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# Backwards-compatible import path. The canonical CallSession lives in
# telephony, which imports TranscriptTurn from here -- so re-exporting it
# eagerly forms a cycle that only resolves when app.core.models happens to be
# imported first. Every entrypoint did that by luck; importing anything under
# app.telephony first (as an audio-only test does) raised ImportError.
# Resolving the name on attribute access keeps `from app.core.models import
# CallSession` working from either direction.
def __getattr__(name: str):  # noqa: E302
    if name == "CallSession":
        from app.telephony.call_session import CallSession

        return CallSession
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
