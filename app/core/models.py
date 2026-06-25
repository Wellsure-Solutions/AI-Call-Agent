from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


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


@dataclass
class CallSession:
    """Runtime state for a single voice-agent call."""

    call_id: str = field(default_factory=lambda: str(uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    status: str = "active"
    transcript: list[TranscriptTurn] = field(default_factory=list)
    answers: dict[str, Any] = field(default_factory=dict)

    def add_turn(self, role: str | None, content: str | None) -> None:
        if not content:
            return
        normalized_role = (role or "agent").strip().lower()
        self.transcript.append(TranscriptTurn(role=normalized_role, content=content.strip()))

    def finish(self, status: str = "completed") -> None:
        self.status = status
        self.ended_at = datetime.now(timezone.utc)

    @property
    def duration_seconds(self) -> int:
        end = self.ended_at or datetime.now(timezone.utc)
        return max(0, int((end - self.started_at).total_seconds()))

    @property
    def transcript_text(self) -> str:
        return "\n".join(f"[{turn.timestamp}] {turn.role}: {turn.content}" for turn in self.transcript)
