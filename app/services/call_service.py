from __future__ import annotations

import logging

from app.services.answer_extractor import AnswerExtractor
from app.storage.excel_store import ExcelAnswerStore
from app.core.models import CallSession

logger = logging.getLogger(__name__)


class CallResultService:
    """Finalizes calls and persists extracted answers.

    This service is adapter-agnostic: browser WebSockets, Asterisk ARI,
    AudioSocket, or future GSM gateway integrations can all pass a completed
    ``CallSession`` here and receive the same extraction/export behavior.
    """

    def __init__(self, extractor: AnswerExtractor, store: ExcelAnswerStore) -> None:
        self.extractor = extractor
        self.store = store

    def finalize(self, session: CallSession, status: str = "completed") -> CallSession:
        if session.ended_at is None:
            session.finish(status)
        else:
            session.status = status
        session.answers = self.extractor.extract(session)
        self.store.append_call(session)
        logger.info("saved_call_answers", extra={"call_id": session.call_id, "status": session.status})
        return session
