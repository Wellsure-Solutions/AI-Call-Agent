from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.telephony.call_session import CallSession


class BaseTelephonyAdapter(ABC):
    """Abstract adapter for browser, Asterisk, SIP, GSM gateway, or future telephony."""

    def __init__(self) -> None:
        self.session: CallSession | None = None

    def attach(self, session: CallSession) -> None:
        self.session = session
        session.telephony_connection = self

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def send_audio(self, pcm_frame: bytes) -> None: ...

    @abstractmethod
    async def receive_audio(self) -> bytes | None: ...

    @abstractmethod
    async def hangup(self) -> None: ...

    @abstractmethod
    async def answer(self) -> None: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    def emit_event(self, name: str, payload: dict[str, Any] | None = None) -> None:
        if self.session is not None:
            self.session.metadata.setdefault("adapter_events", []).append({"name": name, "payload": payload or {}})
