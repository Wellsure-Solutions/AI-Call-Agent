from __future__ import annotations

import logging

from app.services.answer_extractor import AnswerExtractor
from app.services.call_status_tracker import call_status_tracker
from app.storage.json_store import JsonCallStore
from app.core.models import CallSession

logger = logging.getLogger(__name__)


class CallResultService:
    """Finalizes calls and persists extracted answers.

    This service is adapter-agnostic: browser WebSockets, Asterisk ARI,
    AudioSocket, or future GSM gateway integrations can all pass a completed
    ``CallSession`` here and receive the same extraction/export behavior.
    """

    def __init__(self, extractor: AnswerExtractor, store: JsonCallStore) -> None:
        self.extractor = extractor
        self.store = store

    def finalize(self, session: CallSession, status: str = "completed") -> CallSession:
        if session.ended_at is None:
            session.finish(status)
        else:
            session.status = status
        try:
            session.answers = self.extractor.extract(session)
            self.store.append_call(session)
            lead_id = session.metadata.get("lead_id")
            if lead_id:
                self.store.update_lead(lead_id, status=session.status, last_call_id=session.call_id)
            call_status_tracker.upsert(session.call_id, session.status, lead_id=lead_id)
            logger.info("saved_call_answers", extra={"call_id": session.call_id, "status": session.status, "lead_id": lead_id})
        except Exception as exc:
            call_status_tracker.upsert(session.call_id, "result_save_failed", error=str(exc), lead_id=session.metadata.get("lead_id"))
            logger.exception("save_call_answers_failed", extra={"call_id": session.call_id, "status": session.status})
            raise
        return session
