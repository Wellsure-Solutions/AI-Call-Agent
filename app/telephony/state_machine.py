from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CallState(str, Enum):
    CREATED = "CREATED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    AI_ACTIVE = "AI_ACTIVE"
    AI_FINISHED = "AI_FINISHED"
    EXTRACTION = "EXTRACTION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    HUNG_UP = "HUNG_UP"


_TERMINAL_STATES = {CallState.COMPLETED, CallState.FAILED, CallState.HUNG_UP}
_ALLOWED_TRANSITIONS: dict[CallState, set[CallState]] = {
    CallState.CREATED: {CallState.CONNECTING, CallState.FAILED, CallState.HUNG_UP},
    CallState.CONNECTING: {CallState.CONNECTED, CallState.FAILED, CallState.HUNG_UP},
    CallState.CONNECTED: {CallState.AI_ACTIVE, CallState.FAILED, CallState.HUNG_UP},
    CallState.AI_ACTIVE: {CallState.AI_FINISHED, CallState.FAILED, CallState.HUNG_UP},
    CallState.AI_FINISHED: {CallState.EXTRACTION, CallState.COMPLETED, CallState.FAILED, CallState.HUNG_UP},
    CallState.EXTRACTION: {CallState.COMPLETED, CallState.FAILED, CallState.HUNG_UP},
    CallState.COMPLETED: set(),
    CallState.FAILED: set(),
    CallState.HUNG_UP: set(),
}


class InvalidCallStateTransition(RuntimeError):
    """Raised when a call lifecycle transition is not allowed."""


@dataclass
class CallStateMachine:
    state: CallState = CallState.CREATED

    def transition_to(self, next_state: CallState) -> CallState:
        if next_state == self.state:
            return self.state
        if next_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise InvalidCallStateTransition(f"Cannot transition call from {self.state.value} to {next_state.value}.")
        self.state = next_state
        return self.state

    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    def connecting(self) -> CallState:
        return self.transition_to(CallState.CONNECTING)

    def connected(self) -> CallState:
        return self.transition_to(CallState.CONNECTED)

    def ai_active(self) -> CallState:
        return self.transition_to(CallState.AI_ACTIVE)

    def ai_finished(self) -> CallState:
        return self.transition_to(CallState.AI_FINISHED)

    def extraction(self) -> CallState:
        return self.transition_to(CallState.EXTRACTION)

    def completed(self) -> CallState:
        return self.transition_to(CallState.COMPLETED)

    def failed(self) -> CallState:
        return self.transition_to(CallState.FAILED)

    def hung_up(self) -> CallState:
        return self.transition_to(CallState.HUNG_UP)
