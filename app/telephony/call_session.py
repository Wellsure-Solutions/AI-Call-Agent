from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.core.models import TranscriptTurn
from app.telephony.state_machine import CallState, CallStateMachine, InvalidCallStateTransition


@dataclass
class CallSession:
    """Single source of truth for one active or completed call."""

    call_id: str = field(default_factory=lambda: str(uuid4()))
    campaign_name: str = "default"
    phone_number: str | None = None
    direction: str = "browser"
    state_machine: CallStateMachine = field(default_factory=CallStateMachine)
    _status: str = "created"
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: datetime | None = None
    transcript: list[TranscriptTurn] = field(default_factory=list)
    extracted_answers: dict[str, Any] = field(default_factory=dict)
    deepgram_connection: Any | None = None
    telephony_connection: Any | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        self._status = value
        try:
            self.state_machine.state = CallState(value)
        except ValueError:
            try:
                self.state_machine.state = CallState(value.upper())
            except ValueError:
                self.metadata["legacy_status"] = value

    @property
    def started_at(self) -> datetime:
        return self.start_time

    @started_at.setter
    def started_at(self, value: datetime) -> None:
        self.start_time = value

    @property
    def ended_at(self) -> datetime | None:
        return self.end_time

    @ended_at.setter
    def ended_at(self, value: datetime | None) -> None:
        self.end_time = value

    @property
    def answers(self) -> dict[str, Any]:
        return self.extracted_answers

    @answers.setter
    def answers(self, value: dict[str, Any]) -> None:
        self.extracted_answers = value

    @property
    def duration(self) -> int:
        return self.duration_seconds

    def transition_to(self, state: CallState) -> None:
        self.state_machine.transition_to(state)
        self._status = state.value

    def safe_transition_to(self, state: CallState) -> None:
        try:
            self.transition_to(state)
        except InvalidCallStateTransition:
            pass

    def add_turn(self, role: str | None, content: str | None) -> None:
        if not content:
            return
        normalized_role = (role or "agent").strip().lower()
        self.transcript.append(TranscriptTurn(role=normalized_role, content=content.strip()))

    def finish(self, status: str = "completed") -> None:
        status_map = {
            "completed": CallState.COMPLETED,
            "client_disconnected": CallState.HUNG_UP,
            "hung_up": CallState.HUNG_UP,
            "error": CallState.FAILED,
            "failed": CallState.FAILED,
        }
        target = status_map.get(status, CallState.COMPLETED)
        self.safe_transition_to(target)
        self._status = status
        self.end_time = datetime.now(timezone.utc)
        if status not in status_map:
            self.metadata["final_status"] = status

    @property
    def duration_seconds(self) -> int:
        end = self.end_time or datetime.now(timezone.utc)
        return max(0, int((end - self.start_time).total_seconds()))

    @property
    def transcript_text(self) -> str:
        return "\n".join(f"[{turn.timestamp}] {turn.role}: {turn.content}" for turn in self.transcript)
