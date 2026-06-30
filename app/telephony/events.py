from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable


class CallEventType(str, Enum):
    CALL_STARTED = "CallStarted"
    CALL_CONNECTED = "CallConnected"
    TRANSCRIPT_RECEIVED = "TranscriptReceived"
    ANSWER_EXTRACTED = "AnswerExtracted"
    CALL_FINISHED = "CallFinished"
    CALL_FAILED = "CallFailed"


@dataclass(frozen=True)
class CallEvent:
    type: CallEventType
    call_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


EventHandler = Callable[[CallEvent], None]
