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


# Backwards-compatible import path. The canonical CallSession lives in telephony.
from app.telephony.call_session import CallSession  # noqa: E402
