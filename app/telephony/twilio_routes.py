"""
Twilio routes -- add to app.main alongside the existing /ws route:

    from app.telephony.twilio_routes import media_router, router as twilio_router
    app.include_router(twilio_router)
    app.include_router(media_router)

Three routes, matching the three points where Twilio and your server talk
to each other:

  POST /twilio/outbound      -- you call this to start a sales call
  POST /twilio/twiml          -- Twilio calls this once the call is answered
  WS   /media-stream          -- Twilio connects here for live audio
  POST /twilio/status         -- Twilio posts call lifecycle events here
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from app.services.answer_extractor import AnswerExtractor
from app.services.call_service import CallResultService
from app.storage.excel_store import ExcelAnswerStore
from app.core.settings import ANSWERS_WORKBOOK, PUBLIC_BASE_URL
from app.telephony.adapters.twilio_adapter import TwilioAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.call_manager import CallManager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/twilio", tags=["twilio"])
media_router = APIRouter(tags=["twilio-media"])

_answer_extractor = AnswerExtractor()
_answer_store = ExcelAnswerStore(ANSWERS_WORKBOOK)
call_result_service = CallResultService(_answer_extractor, _answer_store)
call_manager = CallManager()


class OutboundCallRequest(BaseModel):
    phone_number: str
    campaign_name: str = "twilio_outbound"


@router.post("/outbound")
async def start_outbound_call(request: OutboundCallRequest):
    """Trigger an outbound sales call. The actual audio/AI loop only starts
    once Twilio's WebSocket connects (see /media-stream below)."""
    session = call_manager.create_session(
        campaign_name=request.campaign_name,
        phone_number=request.phone_number,
        direction="twilio",
    )
    bridge = AudioBridge(session, call_result_service)
    adapter = TwilioAdapter(audio_bridge=bridge)
    adapter.attach(session)

    try:
        await adapter.connect()
    except Exception as exc:
        call_manager.mark_failed(session, str(exc))
        call_manager.destroy_session(session.call_id)
        raise

    return {"call_id": session.call_id, "call_sid": adapter.call_sid}


@router.post("/twiml")
async def twiml_webhook(request: Request):
    """Return TwiML immediately when Twilio asks how to bridge the call.

    This route intentionally does not parse Twilio's POST body, query a
    database, initialize AI services, or wait for the media WebSocket. Twilio
    has a short webhook budget here; the only job is to produce valid TwiML
    that tells Twilio where to open the independent secure WebSocket stream.
    """
    logger.info("TwiML requested from %s", request.client.host if request.client else "unknown")
    ws_url = router_ws_url(request)
    twiml = TwilioAdapter.build_twiml(stream_ws_url=ws_url)
    return PlainTextResponse(content=twiml, media_type="application/xml")


@router.post("/status")
async def status_webhook():
    """Acknowledge Twilio status callbacks without doing work inline.

    Twilio treats status callbacks as time-sensitive webhooks too. Return 200
    first; any durable status processing should be moved to a queue or
    background task that is not on Twilio's request/response path.
    """
    return Response(status_code=200)


@media_router.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()

    # Twilio's first message is "connected"; the second is "start", which
    # carries the callSid and streamSid needed to bind audio to this socket.
    call_sid: str | None = None
    stream_sid: str | None = None
    while call_sid is None:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=5)
        msg = json.loads(raw)
        if msg.get("event") == "start":
            call_sid = msg["start"]["callSid"]
            stream_sid = msg["start"]["streamSid"]

    adapter = TwilioAdapter._pending.pop(call_sid, None)
    if adapter is None or adapter.session is None:
        logger.warning(
            "No pending TwilioAdapter for call_sid=%s; creating media-stream session",
            call_sid,
        )
        session = call_manager.create_session(
            campaign_name="twilio_inbound_or_recovered",
            direction="twilio",
            metadata={"call_sid": call_sid},
        )
        bridge = AudioBridge(session, call_result_service)
        adapter = TwilioAdapter(audio_bridge=bridge)
        adapter.attach(session)
        adapter.call_sid = call_sid

    adapter.websocket = websocket
    adapter.stream_sid = stream_sid
    call_manager.mark_connected(adapter.session)

    try:
        await adapter.start()
    finally:
        call_manager.destroy_session(adapter.session.call_id)
        logger.info("Twilio call %s finished", adapter.session.call_id)


def router_ws_url(request: Request) -> str:
    """Builds the wss:// URL Twilio should stream audio to, from PUBLIC_BASE_URL."""
    base = PUBLIC_BASE_URL
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    return f"{ws_base}/media-stream"
