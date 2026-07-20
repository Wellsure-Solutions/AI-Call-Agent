from __future__ import annotations

import asyncio
import logging

from app.services.answer_extractor import AnswerExtractor
from app.services.call_status_tracker import call_status_tracker
from typing import Any
from app.core.models import CallSession

logger = logging.getLogger(__name__)


class CallResultService:
    """Finalizes calls and persists extracted answers.

    This service is adapter-agnostic: browser WebSockets, Asterisk ARI,
    AudioSocket, or future GSM gateway integrations can all pass a completed
    ``CallSession`` here and receive the same extraction/export behavior.
    """

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
        # Raw data is committed before any fallible provider work.
        durable = hasattr(self.store, "persist_raw")
        if durable:
            self.store.persist_raw(session, status)
        try:
            session.answers = self.extractor.extract(session)
            if hasattr(self.store, "save_extraction"):
                self.store.save_extraction(session.call_id, session.answers)
            elif not durable:
                self.store.append_call(session)
            lead_id = session.metadata.get("lead_id")
            if lead_id:
                self.store.update_lead(lead_id, status=session.status, last_call_id=session.call_id)
            call_status_tracker.upsert(session.call_id, session.status, lead_id=lead_id)
            logger.info("saved_call_answers", extra={"call_id": session.call_id, "status": session.status, "lead_id": lead_id})
        except Exception as exc:
            call_status_tracker.upsert(session.call_id, "result_save_failed", error=str(exc), lead_id=session.metadata.get("lead_id"))
            logger.exception("save_call_answers_failed", extra={"call_id": session.call_id, "status": session.status})
            if hasattr(self.store, "extraction_failed"):
                self.store.extraction_failed(session.call_id, type(exc).__name__, permanent=False)
            # Post-call extraction must never tear down the media WebSocket.
        return session

    async def afinalize(self, session: CallSession, status: str = "completed") -> CallSession:
        if session.ended_at is None:
            session.finish(status)
        else:
            session.status = status
        if hasattr(self.store, "persist_raw"):
            await asyncio.to_thread(self.store.persist_raw, session, status)
        else:
            await asyncio.to_thread(self.store.append_call, session)
        for attempt in range(1, self.max_attempts + 1):
            try:
                answers = await asyncio.wait_for(asyncio.to_thread(self.extractor.extract, session), self.timeout)
                session.answers = answers
                if hasattr(self.store, "save_extraction"):
                    await asyncio.to_thread(self.store.save_extraction, session.call_id, answers)
                break
            except Exception as exc:
                permanent = attempt == self.max_attempts
                logger.warning("call_extraction_failed", extra={"call_id": session.call_id, "error_type": type(exc).__name__, "attempt": attempt})
                if hasattr(self.store, "extraction_failed"):
                    await asyncio.to_thread(self.store.extraction_failed, session.call_id, type(exc).__name__, permanent)
                if not permanent:
                    await asyncio.sleep(min(2 ** (attempt - 1), 5))
        return session
