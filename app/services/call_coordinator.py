from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from twilio.base.exceptions import TwilioRestException
from uuid import uuid4

from app.services.answer_extractor import AnswerExtractor
from app.storage.sqlite_store import PROVIDER_TERMINAL, SQLiteCallStore
from app.telephony.adapters.twilio_adapter import TwilioAdapter
from app.telephony.call_session import CallSession

logger = logging.getLogger(__name__)


class DurableCallCoordinator:
    """Database-coordinated dial, deadline/reconciliation, and extraction workers."""

    def __init__(self, store: SQLiteCallStore, maximum: int, start_interval: float, *, ring_timeout: int = 45,
                 max_call_seconds: int = 900, extraction_timeout: float = 30, extraction_retry_delay: float = 5,
                 extractor: AnswerExtractor | None = None, adapter_factory=TwilioAdapter) -> None:
        self.store = store
        self.maximum = maximum
        self.start_interval = start_interval
        self.ring_timeout = ring_timeout
        self.max_call_seconds = max_call_seconds
        self.extraction_timeout = extraction_timeout
        self.extraction_retry_delay = extraction_retry_delay
        self.extractor = extractor or AnswerExtractor()
        self.adapter_factory = adapter_factory
        self.owner = str(uuid4())
        self._stop = asyncio.Event()
        self.last_iteration_at: str | None = None
        self.last_error: str | None = None
        self.iterations = 0

    async def run(self) -> None:
        backoff = 0.25
        while not self._stop.is_set():
            try:
                await self.run_once()
                backoff = 0.25
            except Exception as error:
                self.last_error = type(error).__name__
                logger.exception("coordinator_iteration_failed", extra={"error_type": type(error).__name__})
                await self._sleep(backoff)
                backoff = min(backoff * 2, 10)

    async def run_once(self) -> None:
        self.iterations += 1
        self.last_iteration_at = datetime.now(timezone.utc).isoformat()
        action = await asyncio.to_thread(self.store.claim_due_action, self.owner)
        if action:
            await self._reconcile(action)
        extraction = await asyncio.to_thread(self.store.claim_extraction, self.owner, int(self.extraction_timeout) + 30)
        if extraction:
            await self._extract(extraction)
        call = await asyncio.to_thread(self.store.claim_job, self.owner, self.maximum)
        if call:
            if self._stop.is_set():
                await asyncio.to_thread(self.store.relinquish_claim, call["call_id"], self.owner)
            else:
                await self._dial(call)
                await self._sleep(self.start_interval)
        elif not action and not extraction:
            await self._sleep(0.25)

    async def _dial(self, call: dict) -> None:
        adapter = None
        try:
            session = CallSession(call_id=call["call_id"], phone_number=call["phone_number"], direction="twilio",
                metadata={key: call.get(key) for key in ("lead_id", "business_name", "category", "notes")})
            adapter = self.adapter_factory()
            adapter.ring_timeout = self.ring_timeout
            adapter.attach(session)
            await adapter.connect()
            sid = adapter.call_sid
            if not sid or not await asyncio.to_thread(self.store.bind_call_sid, call["call_id"], sid, self.ring_timeout, self.max_call_seconds):
                await asyncio.to_thread(self.store.mark_dial_ambiguous, call["call_id"], "sid_binding_conflict")
                if sid:
                    await adapter.hangup()
                return
        except Exception as error:
            # Twilio transport/API exceptions can be ambiguous after submission. Never blind-redial.
            try:
                if isinstance(error, TwilioRestException) and isinstance(error.status, int) and 400 <= error.status < 500:
                    await asyncio.to_thread(self.store.mark_dial_rejected, call["call_id"], type(error).__name__)
                else:
                    await asyncio.to_thread(self.store.mark_dial_ambiguous, call["call_id"], type(error).__name__)
                if adapter is not None and adapter.call_sid:
                    try:
                        await adapter.hangup()
                    except Exception:
                        logger.exception("uncorrelated_provider_hangup_failed", extra={"call_id": call.get("call_id")})
            except Exception:
                logger.exception("dial_failure_persistence_failed", extra={"call_id": call.get("call_id")})

    async def _reconcile(self, call: dict) -> None:
        try:
            adapter = self.adapter_factory()
            status = await adapter.fetch_status(call["call_sid"])
            if status in PROVIDER_TERMINAL:
                await asyncio.to_thread(self.store.reconciliation_result, call["call_id"], self.owner, status)
                return
            # Ringing/queued calls are canceled; connected calls are completed.
            requested = "canceled" if status in {"queued", "ringing", "initiated"} else "completed"
            await adapter.update_status(call["call_sid"], requested)
            # Capacity remains occupied pending terminal webhook or a later terminal lookup.
            await asyncio.to_thread(self.store.reconciliation_result, call["call_id"], self.owner, None, "Provider termination requested")
        except Exception as error:
            await asyncio.to_thread(self.store.reconciliation_result, call["call_id"], self.owner, None, type(error).__name__)

    async def _extract(self, call: dict) -> None:
        session = CallSession(call_id=call["call_id"], phone_number=call["phone_number"], metadata={"lead_id": call.get("lead_id")})
        # Extraction consumes the durable transcript directly without logging it.
        session.add_turn("transcript", call.get("transcript") or "[empty transcript]")
        try:
            answers = await self.extractor.extract_async(session, self.extraction_timeout)
            await asyncio.to_thread(self.store.complete_extraction, call["call_id"], self.owner, answers)
        except Exception as error:
            await asyncio.to_thread(self.store.fail_extraction, call["call_id"], self.owner, type(error).__name__, self.extraction_retry_delay)

    async def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def health(self) -> dict[str, object]:
        return {"owner": self.owner, "running": not self._stop.is_set(), "iterations": self.iterations,
                "last_iteration_at": self.last_iteration_at, "last_error": self.last_error}

    def stop(self) -> None:
        self._stop.set()
