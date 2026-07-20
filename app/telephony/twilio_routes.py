from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, WebSocket
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from twilio.request_validator import RequestValidator

from app.core.settings import MAX_CALL_SECONDS, PUBLIC_BASE_URL, RING_TIMEOUT_SECONDS, STREAM_SECRET, TWILIO_AUTH_TOKEN
from app.services.answer_extractor import AnswerExtractor
from app.services.call_service import CallResultService
from app.storage.sqlite_store import SQLiteCallStore, SuppressedError
from app.telephony.adapters.twilio_adapter import TwilioAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.call_session import CallSession

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
    if not await _valid_signature(request, form): raise HTTPException(403, "Invalid Twilio signature")
    call = await _repo().aget_call(call_id); sid = str(form.get("CallSid") or "")
    if not call or not sid or not await asyncio.to_thread(_repo().bind_call_sid, call_id, sid, RING_TIMEOUT_SECONDS, MAX_CALL_SECONDS): raise HTTPException(409, "Call correlation failed")
    expiry = int(time.time()) + 300
    ws_base = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    xml = TwilioAdapter.build_twiml(f"{ws_base}/media-stream", {"call_id":call_id,"expiry":str(expiry),"token":stream_token(call_id,sid,expiry)})
    return PlainTextResponse(xml, media_type="application/xml")

@router.post("/status/{call_id}")
async def status_webhook(call_id: str, request: Request):
    form = dict(await request.form())
    if not await _valid_signature(request, form): raise HTTPException(403, "Invalid Twilio signature")
    sid, status = str(form.get("CallSid") or ""), str(form.get("CallStatus") or "").lower()
    if sid and status: await _repo().aprovider_status(call_id, status, sid)
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
    bridge=AudioBridge(session,_result_service); adapter=TwilioAdapter(bridge); adapter.attach(session); adapter.call_sid=sid; adapter.stream_sid=start.get("streamSid"); adapter.websocket=websocket
    try: await adapter.start()
    finally:
        if session.ended_at is None: await bridge.stop("completed")
