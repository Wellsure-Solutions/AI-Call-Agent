"""
Twilio routes -- add to app.main alongside the existing /ws route:

    from app.telephony.twilio_routes import router as twilio_router
    app.include_router(twilio_router)

Three routes, matching the three points where Twilio and your server talk
to each other:

  POST /twilio/outbound      -- you call this to start a sales call
  POST /twilio/twiml          -- Twilio calls this once the call is answered
  WS   /twilio/media-stream   -- Twilio connects here for live audio
  POST /twilio/status         -- Twilio posts call lifecycle events here
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import PlainTextResponse
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
    """Twilio fetches this once the call is answered."""
    form = await request.form()
    call_sid = form.get("CallSid")
    logger.info("TwiML requested for call_sid=%s", call_sid)

    ws_url = router_ws_url(request)
    twiml = TwilioAdapter.build_twiml(stream_ws_url=ws_url)
    return PlainTextResponse(content=twiml, media_type="application/xml")


@router.post("/status")
async def status_webhook(request: Request):
    form = await request.form()
    logger.info("Twilio status callback: %s", dict(form))
    return PlainTextResponse("")


@router.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()

    # Twilio's first message is "connected"; the second is "start", which
    # carries the callSid we registered in TwilioAdapter._pending back in
    # /outbound, plus the streamSid needed to send audio back.
    call_sid: str | None = None
    stream_sid: str | None = None
    while call_sid is None:
        raw = await websocket.receive_text()
        msg = json.loads(raw)
        if msg.get("event") == "start":
            call_sid = msg["start"]["callSid"]
            stream_sid = msg["start"]["streamSid"]

    adapter = TwilioAdapter._pending.pop(call_sid, None)
    if adapter is None or adapter.session is None:
        logger.warning("No pending TwilioAdapter for call_sid=%s -- closing socket", call_sid)
        await websocket.close()
        return

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
    return f"{ws_base}/twilio/media-stream"