from __future__ import annotations

import logging

from app.services.answer_extractor import AnswerExtractor
from app.storage.excel_store import ExcelAnswerStore
from app.storage.json_storage import JsonStorage
from app.core.models import CallSession

logger = logging.getLogger(__name__)


class CallResultService:
    """Finalizes calls and persists extracted answers.

    This service is adapter-agnostic: browser WebSockets, Asterisk ARI,
    AudioSocket, or future GSM gateway integrations can all pass a completed
    ``CallSession`` here and receive the same extraction/export behavior.
    """

    def __init__(self, extractor: AnswerExtractor, store: ExcelAnswerStore, json_store: JsonStorage | None = None) -> None:
        self.extractor = extractor
        self.store = store
        self.json_store = json_store or JsonStorage()

    def finalize(self, session: CallSession, status: str = "completed") -> CallSession:
        if session.ended_at is None:
            session.finish(status)
        else:
            session.status = status
        session.answers = self.extractor.extract(session)
        self._save_json(session)
        self.store.append_call(session)
        logger.info("saved_call_answers", extra={"call_id": session.call_id, "status": session.status})
        return session

    def _save_json(self, session: CallSession) -> None:
        lead = session.metadata.get("lead", {})
        call = {
            "id": session.call_id,
            "campaign": session.campaign_name,
            "campaign_id": session.metadata.get("campaign_id"),
            "business_name": lead.get("business_name"),
            "phone": session.phone_number or lead.get("phone"),
            "category": lead.get("category"),
            "status": session.status,
            "outcome": session.answers.get("interest_level") or session.answers.get("interested") or session.status,
            "duration": session.duration_seconds,
            "started_at": session.started_at.isoformat(),
            "completed_at": session.ended_at.isoformat() if session.ended_at else None,
            "transcript": session.transcript_text,
            "events": session.metadata.get("events", []),
            "system_prompt": session.metadata.get("system_prompt"),
            "model": session.metadata.get("model"),
            "temperature": session.metadata.get("temperature"),
            "errors": session.metadata.get("errors", []),
        }
        response = {
            "id": session.call_id,
            "call_id": session.call_id,
            "business_name": call["business_name"],
            "phone": call["phone"],
            "category": call["category"],
            "interested": session.answers.get("interest_level") in {"hot", "warm", True} or session.answers.get("interested") in {"yes", True},
            "callback_requested": session.answers.get("callback_approved") == "yes",
            "callback_time": session.answers.get("callback_time"),
            "summary": session.answers.get("summary", ""),
            "sentiment": session.answers.get("sentiment", "neutral"),
            **session.answers,
        }
        existing = self.json_store.get("calls", session.call_id)
        if existing:
            self.json_store.update("calls", session.call_id, call)
        else:
            self.json_store.create("calls", call)
        if self.json_store.get("responses", session.call_id):
            self.json_store.update("responses", session.call_id, response)
        else:
            self.json_store.create("responses", response)
