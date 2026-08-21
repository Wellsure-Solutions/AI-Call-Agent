from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

from app.services.answer_extractor import AnswerExtractor
from app.storage.sqlite_store import PROVIDER_TERMINAL, SQLiteCallStore
from app.telephony.call_session import CallSession
from app.telephony.callback_urls import media_stream_url, status_callback_url
from app.telephony.providers import DEFAULT_PROVIDER, get_provider
from app.telephony.providers.base import DialResult

logger = logging.getLogger(__name__)


class _LegacyAdapterProvider:
    """Adapts the old duck-typed `adapter_factory` to `TelephonyProvider`.

    The coordinator used to build a `TwilioAdapter` and call
    `connect()`/`fetch_status()`/`update_status()` on it, and tests inject
    fakes with exactly that shape. Those fakes are the cheapest way to drive
    the queue without a carrier, so the entry point is kept rather than
    rewritten -- this shim translates, and implements the same
    rejected/ambiguous rule the coordinator used to apply inline.

    Only used when a caller explicitly passes `adapter_factory`. Production
    goes through the real providers, keyed on the call's persisted provider.
    """

    name = "legacy"
    supports_amd = False

    def __init__(self, factory, ring_timeout: int) -> None:
        self._factory = factory
        self._ring_timeout = ring_timeout
        self.adapter = None

    async def dial(self, *, call_id: str, to_number: str, ring_timeout: int,
                   stream_url: str = "", status_callback_url: str = "") -> DialResult:
        session = CallSession(call_id=call_id, phone_number=to_number, direction="twilio")
        adapter = self._factory()
        self.adapter = adapter
        adapter.ring_timeout = ring_timeout
        adapter.attach(session)
        await adapter.connect()
        return DialResult(getattr(adapter, "call_sid", None), None)

    async def fetch_status(self, provider_sid: str) -> str:
        return await self._factory().fetch_status(provider_sid)

    async def request_terminal(self, provider_sid: str, requested: str) -> None:
        await self._factory().update_status(provider_sid, requested)

    def terminal_request_for(self, status: str) -> str:
        from app.telephony.providers.twilio_provider import TWILIO_PRE_ANSWER
        from app.telephony.providers.base import terminal_request_for

        return terminal_request_for(status, TWILIO_PRE_ANSWER)

    async def abandon(self, provider_sid: str) -> None:
        """Hang up a call we placed but could not correlate."""
        adapter = self.adapter
        if adapter is not None and getattr(adapter, "call_sid", None):
            await adapter.hangup()

    def classify_dial_error(self, error: Exception) -> str:
        from app.telephony.providers.twilio_provider import classify_twilio_error

        return classify_twilio_error(error)


class DurableCallCoordinator:
    """Database-coordinated dial, deadline/reconciliation, and extraction workers."""

    def __init__(self, store: SQLiteCallStore, maximum: int, start_interval: float, *, ring_timeout: int = 45,
                 max_call_seconds: int = 900, extraction_timeout: float = 30, extraction_retry_delay: float = 5,
                 extractor: AnswerExtractor | None = None, adapter_factory=None,
                 reconciliation_max_attempts: int = 8, abandoned_grace_seconds: float = 300.0,
                 provider_factory=get_provider) -> None:
        self.store = store
        self.maximum = maximum
        self.start_interval = start_interval
        self.ring_timeout = ring_timeout
        self.max_call_seconds = max_call_seconds
        self.extraction_timeout = extraction_timeout
        self.extraction_retry_delay = extraction_retry_delay
        self.extractor = extractor or AnswerExtractor()
        self.adapter_factory = adapter_factory
        self.provider_factory = provider_factory
        self.reconciliation_max_attempts = reconciliation_max_attempts
        self.abandoned_grace_seconds = abandoned_grace_seconds
        self.quarantined = 0
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
        # Release capacity held by calls that no deadline action can still
        # resolve. A single one of those blocks every queued call at the
        # default concurrency of one.
        #
        # Isolated deliberately: this is housekeeping, and letting it fail the
        # iteration would stop dialling entirely -- reproducing, via a
        # different route, exactly the stall it exists to clear.
        try:
            released = await asyncio.to_thread(self.store.quarantine_abandoned_jobs, self.abandoned_grace_seconds)
        except Exception as error:
            released = 0
            logger.warning("job_quarantine_sweep_failed", extra={"error_type": type(error).__name__})
        if released:
            self.quarantined += released
            logger.warning("jobs_quarantined", extra={"count": released})
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

    def _provider_for(self, call: dict):
        """Resolve the control plane for one call.

        Keyed on the provider persisted on the *call row*, never on the
        currently-selected setting. An operator flipping the toggle mid-flight
        must not make this ask Exotel about a Twilio CallSid: that lookup
        fails, burns every reconciliation attempt, and quarantines a healthy
        call while it holds a capacity slot -- at the default concurrency of
        one, a full queue stall.
        """
        if self.adapter_factory is not None:
            return _LegacyAdapterProvider(self.adapter_factory, self.ring_timeout)
        return self.provider_factory(call.get("provider") or DEFAULT_PROVIDER)

    async def _dial(self, call: dict) -> None:
        provider = self._provider_for(call)
        sid: str | None = None
        try:
            result = await provider.dial(
                call_id=call["call_id"],
                to_number=call["phone_number"],
                ring_timeout=self.ring_timeout,
                stream_url=media_stream_url(getattr(provider, "name", DEFAULT_PROVIDER), call["call_id"]),
                status_callback_url=status_callback_url(getattr(provider, "name", DEFAULT_PROVIDER), call["call_id"]),
            )
            sid = result.provider_sid
            if not sid or not await asyncio.to_thread(self.store.bind_call_sid, call["call_id"], sid, self.ring_timeout, self.max_call_seconds):
                await asyncio.to_thread(self.store.mark_dial_ambiguous, call["call_id"], "sid_binding_conflict")
                if sid:
                    await self._abandon(provider, sid, call)
                return
        except Exception as error:
            # Transport/API exceptions can be ambiguous after submission, and
            # only the provider knows which of its own failures are proven
            # refusals. Never blind-redial.
            try:
                if provider.classify_dial_error(error) == "rejected":
                    await asyncio.to_thread(self.store.mark_dial_rejected, call["call_id"], type(error).__name__)
                else:
                    await asyncio.to_thread(self.store.mark_dial_ambiguous, call["call_id"], type(error).__name__)
                if sid:
                    await self._abandon(provider, sid, call)
            except Exception:
                logger.exception("dial_failure_persistence_failed", extra={"call_id": call.get("call_id")})

    @staticmethod
    async def _abandon(provider, sid: str, call: dict) -> None:
        """End a call we placed but could not correlate to a durable row."""
        try:
            abandon = getattr(provider, "abandon", None)
            if abandon is not None:
                await abandon(sid)
            else:
                await provider.request_terminal(sid, "completed")
        except Exception:
            logger.exception("uncorrelated_provider_hangup_failed", extra={"call_id": call.get("call_id")})

    async def _reconcile(self, call: dict) -> None:
        try:
            provider = self._provider_for(call)
            status = await provider.fetch_status(call["call_sid"])
            if status in PROVIDER_TERMINAL:
                await asyncio.to_thread(self.store.reconciliation_result, call["call_id"], self.owner, status,
                                        None, self.reconciliation_max_attempts)
                return
            # Pre-answer calls are canceled; connected calls are completed.
            # Which status words mean "not answered yet" is carrier-specific
            # (Twilio says `ringing`, Exotel has no such status), so the
            # provider owns the mapping.
            requested = provider.terminal_request_for(status)
            await provider.request_terminal(call["call_sid"], requested)
            # Capacity remains occupied pending terminal webhook or a later terminal lookup.
            await asyncio.to_thread(self.store.reconciliation_result, call["call_id"], self.owner, None,
                                    "Provider termination requested", self.reconciliation_max_attempts)
        except Exception as error:
            await asyncio.to_thread(self.store.reconciliation_result, call["call_id"], self.owner, None,
                                    type(error).__name__, self.reconciliation_max_attempts)

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
                "last_iteration_at": self.last_iteration_at, "last_error": self.last_error,
                "quarantined_jobs": self.quarantined}

    def stop(self) -> None:
        self._stop.set()
