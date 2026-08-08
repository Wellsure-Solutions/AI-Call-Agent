from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, WebSocket
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from twilio.request_validator import RequestValidator

from app.core.settings import (
    AMD_TERMINAL_VERDICTS,
    BARGE_IN_VOICE_ENERGY_THRESHOLD,
    MAX_CALL_SECONDS,
    MEDIA_DUMP_DIR,
    METRICS_ENABLED,
    METRICS_FLUSH_SECONDS,
    METRICS_SILENCE_GAP_MS,
    PUBLIC_BASE_URL,
    RING_TIMEOUT_SECONDS,
    STREAM_SECRET,
    TWILIO_AUTH_TOKEN,
)
from app.services.answer_extractor import AnswerExtractor
from app.services.call_service import CallResultService
from app.storage.sqlite_store import SQLiteCallStore, SuppressedError
from app.telephony.adapters.twilio_adapter import TwilioAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.integrations.deepgram.config import cached_greeting_audio
from app.telephony.audio.media_dump import MediaDump
from app.telephony.call_session import CallSession
from app.telephony.metrics import CallMetrics, MetricsWriter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/twilio", tags=["twilio"])
media_router = APIRouter(tags=["twilio-media"])
TERMINAL_CALL_STATUSES = {"completed", "failed", "busy", "no-answer", "canceled"}
_store: SQLiteCallStore | None = None
_result_service: CallResultService | None = None

class OutboundCallRequest(BaseModel):
    phone_number: str
    campaign_name: str = "twilio_outbound"
    lead_id: str | None = None
    business_name: str | None = None
    category: str | None = None
    notes: str | None = None

def configure(store: SQLiteCallStore, result_service: CallResultService) -> None:
    global _store, _result_service
    _store, _result_service = store, result_service

def _repo() -> SQLiteCallStore:
    if _store is None: raise RuntimeError("Twilio repository is not configured")
    return _store

def _external_url(request: Request) -> str:
    return f"{PUBLIC_BASE_URL}{request.url.path}"

async def _valid_signature(request: Request, form: dict) -> bool:
    if not TWILIO_AUTH_TOKEN or not PUBLIC_BASE_URL: return False
    signature = request.headers.get("X-Twilio-Signature", "")
    return RequestValidator(TWILIO_AUTH_TOKEN).validate(_external_url(request), form, signature)


# A wrong PUBLIC_BASE_URL or auth token rejects *every* Twilio callback, and
# each rejection is individually indistinguishable from a forged request --
# so the failure mode is total and completely silent. TwiML never renders, so
# no call produces media; status callbacks never land, so each job holds its
# concurrency slot until the maximum-call deadline reconciles it. Counting
# rejections turns that into something an operator can see.
_signature_failures: dict[str, Any] = {"total": 0, "by_endpoint": {}, "last_at": None, "last_endpoint": None}


def _record_signature_failure(endpoint: str, call_id: str) -> None:
    _signature_failures["total"] += 1
    _signature_failures["by_endpoint"][endpoint] = _signature_failures["by_endpoint"].get(endpoint, 0) + 1
    _signature_failures["last_at"] = datetime.now(timezone.utc).isoformat()
    _signature_failures["last_endpoint"] = endpoint
    # Logged at warning with the configured origin, because the overwhelmingly
    # likely cause is a mismatch between it and the URL Twilio actually
    # signed. The token itself is never logged.
    logger.warning(
        "twilio_signature_rejected",
        extra={
            "endpoint": endpoint,
            "call_id": call_id,
            "configured_public_base_url": PUBLIC_BASE_URL or "<unset>",
            "total_rejections": _signature_failures["total"],
        },
    )


def signature_failure_health() -> dict[str, Any]:
    """Surfaced on /api/operations and /health so a total outage is visible."""
    return dict(_signature_failures, by_endpoint=dict(_signature_failures["by_endpoint"]))

def stream_token(call_id: str, sid: str, expiry: int) -> str:
    if not STREAM_SECRET: return ""
    return hmac.new(STREAM_SECRET.encode(), f"{call_id}:{sid}:{expiry}".encode(), hashlib.sha256).hexdigest()

def valid_stream_token(call_id: str, sid: str, expiry: int, token: str) -> bool:
    return bool(STREAM_SECRET and expiry >= int(time.time()) and hmac.compare_digest(stream_token(call_id, sid, expiry), token))

@router.post("/outbound")
async def start_outbound_call(request: OutboundCallRequest, idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    try:
        call = await _repo().aenqueue_call(phone_number=request.phone_number, lead_id=request.lead_id, business_name=request.business_name or "", category=request.category or "", notes=request.notes or "", idempotency_key=idempotency_key)
    except SuppressedError as exc: raise HTTPException(409, str(exc)) from exc
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc
    return {"call_id": call["call_id"], "call_sid": call.get("call_sid"), "status": "queued"}

@router.post("/twiml/{call_id}")
async def twiml_webhook(call_id: str, request: Request):
    form = dict(await request.form())
    if not await _valid_signature(request, form):
        _record_signature_failure("twiml", call_id)
        raise HTTPException(403, "Invalid Twilio signature")
    call = await _repo().aget_call(call_id); sid = str(form.get("CallSid") or "")
    if not call or not sid or not await asyncio.to_thread(_repo().bind_call_sid, call_id, sid, RING_TIMEOUT_SECONDS, MAX_CALL_SECONDS): raise HTTPException(409, "Call correlation failed")
    expiry = int(time.time()) + 300
    ws_base = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    xml = TwilioAdapter.build_twiml(f"{ws_base}/media-stream", {"call_id":call_id,"expiry":str(expiry),"token":stream_token(call_id,sid,expiry)})
    return PlainTextResponse(xml, media_type="application/xml")

@router.post("/status/{call_id}")
async def status_webhook(call_id: str, request: Request):
    form = dict(await request.form())
    if not await _valid_signature(request, form):
        _record_signature_failure("status", call_id)
        raise HTTPException(403, "Invalid Twilio signature")
    sid, status = str(form.get("CallSid") or ""), str(form.get("CallStatus") or "").lower()
    if sid and status: await _repo().aprovider_status(call_id, status, sid)
    return Response(status_code=200)

@router.post("/amd/{call_id}")
async def amd_webhook(call_id: str, request: Request):
    """Twilio's asynchronous answering-machine verdict.

    The call is already live and pitching by the time this arrives -- that is
    the trade for not making every human answer wait on detection. Acting on
    it here saves the rest of the call: the remaining per-minute charge, the
    concurrency slot, and a voicemail greeting being fed into extraction as
    if it were a customer.

    On a machine, Twilio is asked to complete the call. Capacity is *not*
    released here; the resulting terminal status webhook does that, as it
    does for every other ending.
    """
    form = dict(await request.form())
    if not await _valid_signature(request, form):
        _record_signature_failure("amd", call_id)
        raise HTTPException(403, "Invalid Twilio signature")
    sid = str(form.get("CallSid") or "")
    answered_by = str(form.get("AnsweredBy") or "").lower()
    if not answered_by:
        return Response(status_code=200)

    call = await asyncio.to_thread(_repo().record_answered_by, call_id, answered_by, sid or None)
    if call is None:
        return Response(status_code=200)
    if answered_by in AMD_TERMINAL_VERDICTS and sid:
        try:
            await TwilioAdapter().update_status(sid, "completed")
        except Exception:
            # The deadline reconciler still owns this call and will retry;
            # failing the webhook would only make Twilio redeliver it.
            logger.warning("amd_hangup_failed", extra={"call_id": call_id, "answered_by": answered_by})
    return Response(status_code=200)


@media_router.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        start = None
        for _ in range(3):
            msg=json.loads(await asyncio.wait_for(websocket.receive_text(),5))
            if msg.get("event")=="start": start=msg["start"]; break
        if not start: raise ValueError("missing start event")
        params=start.get("customParameters") or {}; call_id=str(params.get("call_id") or ""); sid=str(start.get("callSid") or "")
        try: expiry=int(params.get("expiry") or 0)
        except (TypeError,ValueError): expiry=0
        if not valid_stream_token(call_id,sid,expiry,str(params.get("token") or "")): raise ValueError("invalid stream token")
        call=await _repo().aclaim_media(call_id,sid,str(uuid4()))
        if not call: raise ValueError("terminal, unknown, or already-owned call")
    except Exception:
        await websocket.close(code=1008); return
    session=CallSession(call_id=call_id,campaign_name="twilio_outbound",phone_number=call["phone_number"],direction="twilio",metadata={"lead_id":call.get("lead_id"),"business_name":call.get("business_name"),"category":call.get("category"),"notes":call.get("notes"),"phone_number":call.get("phone_number"),"call_sid":sid,"media_connected":True})
    session.safe_transition_to(__import__('app.telephony.state_machine',fromlist=['CallState']).CallState.CONNECTING)
    session.safe_transition_to(__import__('app.telephony.state_machine',fromlist=['CallState']).CallState.CONNECTED)

    # Instrumentation is built before the bridge so the clock is anchored at
    # the earliest instant this process controls, and torn down last so the
    # final flush still happens on an errored or hung-up call.
    # A cache hit removes the provider round-trip from the start of the call
    # entirely; a miss silently falls back to Deepgram synthesising it.
    greeting = cached_greeting_audio()
    metrics, writer = _build_metrics(call_id, "cached" if greeting else "provider")
    dump = MediaDump.create(MEDIA_DUMP_DIR, call_id)
    if metrics is not None:
        metrics.bind()

    # hard_interrupt=False is required, not optional. TwilioAdapter only
    # applies it when it has to build the bridge itself, and this route always
    # supplies one -- so the default (True) silently won, and every real call
    # ran in hard-cut mode: soft barge-in never executed once. Two symptoms
    # came from that. Barge-in decisions were never recorded because the pause
    # they measure was never begun, and any sound from the customer during the
    # agent's closing line discarded the queued goodbye outright.
    bridge=AudioBridge(session,_result_service,hard_interrupt=False,metrics=metrics,greeting_already_played=bool(greeting)); adapter=TwilioAdapter(bridge,metrics=metrics,media_dump=dump); adapter.pending_greeting=greeting; adapter.attach(session); adapter.call_sid=sid; adapter.stream_sid=start.get("streamSid"); adapter.websocket=websocket
    close_reason = "completed"
    try:
        await adapter.start()
    except Exception:
        close_reason = "error"
        raise
    finally:
        if session.ended_at is None: await bridge.stop("completed")
        if dump is not None: dump.close()
        if metrics is not None and writer is not None:
            writer.sink(metrics.finish(close_reason))
            await writer.stop()


def _build_metrics(call_id: str, greeting_source: str = "provider") -> tuple[CallMetrics | None, MetricsWriter | None]:
    if not METRICS_ENABLED:
        return None, None
    writer = MetricsWriter(_repo(), call_id, flush_interval=METRICS_FLUSH_SECONDS)
    writer.start()
    metrics = CallMetrics(
        call_id,
        writer.sink,
        voice_threshold=BARGE_IN_VOICE_ENERGY_THRESHOLD,
        silence_gap_ms=METRICS_SILENCE_GAP_MS,
        greeting_source=greeting_source,
    )
    return metrics, writer
