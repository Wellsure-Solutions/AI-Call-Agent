from __future__ import annotations

from collections import defaultdict
from typing import DefaultDict

from app.telephony.call_session import CallSession
from app.telephony.events import CallEvent, CallEventType, EventHandler
from app.telephony.state_machine import CallState


class CallManager:
    """Owns call lifecycle, active session lookup, and call events."""

    def __init__(self) -> None:
        self._sessions: dict[str, CallSession] = {}
        self._subscribers: DefaultDict[CallEventType, list[EventHandler]] = defaultdict(list)

    def create_session(
        self,
        campaign_name: str = "default",
        phone_number: str | None = None,
        direction: str = "browser",
        metadata: dict | None = None,
    ) -> CallSession:
        session = CallSession(campaign_name=campaign_name, phone_number=phone_number, direction=direction)
        session.metadata.update(metadata or {})
        self._sessions[session.call_id] = session
        self.emit(CallEventType.CALL_STARTED, session)
        return session

    def get_session(self, call_id: str) -> CallSession | None:
        return self._sessions.get(call_id)

    def destroy_session(self, call_id: str) -> CallSession | None:
        return self._sessions.pop(call_id, None)

    def active_calls(self) -> list[CallSession]:
        return [session for session in self._sessions.values() if not session.state_machine.is_terminal()]

    def subscribe(self, event_type: CallEventType, handler: EventHandler) -> None:
        self._subscribers[event_type].append(handler)

    def emit(self, event_type: CallEventType, session: CallSession, **payload) -> None:
        event = CallEvent(type=event_type, call_id=session.call_id, payload=payload)
        for handler in self._subscribers[event_type]:
            handler(event)

    def mark_connected(self, session: CallSession) -> None:
        session.safe_transition_to(CallState.CONNECTING)
        session.safe_transition_to(CallState.CONNECTED)
        self.emit(CallEventType.CALL_CONNECTED, session)

    def mark_failed(self, session: CallSession, error: str) -> None:
        session.safe_transition_to(CallState.FAILED)
        self.emit(CallEventType.CALL_FAILED, session, error=error)
