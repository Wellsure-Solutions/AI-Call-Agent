from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.services.answer_extractor import AnswerExtractor
from app.telephony.call_session import CallSession

logger = logging.getLogger(__name__)


class CallResultService:
    """Raw-first finalization; durable extraction is handled by its worker."""

    def __init__(self, extractor: AnswerExtractor, store: Any, *, timeout: float = 30, max_attempts: int = 3) -> None:
        self.extractor = extractor
        self.store = store
        self.timeout = timeout
        self.max_attempts = max_attempts

    def finalize(self, session: CallSession, status: str = "completed") -> CallSession:
        if session.ended_at is None:
            session.finish(status)
        else:
            session.status = status
        if hasattr(self.store, "persist_raw"):
            self.store.persist_raw(session, status, self.max_attempts)
        else:
            session.answers = self.extractor.extract(session)
            self.store.append_call(session)
        return session

    async def afinalize(self, session: CallSession, status: str = "completed") -> CallSession:
        if session.ended_at is None:
            session.finish(status)
        else:
            session.status = status
        if hasattr(self.store, "persist_raw"):
            await asyncio.to_thread(self.store.persist_raw, session, status, self.max_attempts)
        else:
            session.answers = await self.extractor.extract_async(session, self.timeout)
            await asyncio.to_thread(self.store.append_call, session)
        logger.info("raw_call_persisted", extra={"call_id": session.call_id, "media_end_reason": status})
        return session
