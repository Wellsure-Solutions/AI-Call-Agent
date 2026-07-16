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

import json
import logging

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from app.services.answer_extractor import AnswerExtractor
from app.services.call_service import CallResultService
from app.services.call_status_tracker import call_status_tracker
from app.storage.json_store import JsonCallStore
from app.core.settings import DATA_DIR, PUBLIC_BASE_URL
from app.telephony.adapters.twilio_adapter import TwilioAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.call_manager import CallManager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/twilio", tags=["twilio"])
media_router = APIRouter(tags=["twilio-media"])

_answer_extractor = AnswerExtractor()
_answer_store = JsonCallStore(DATA_DIR)
call_result_service = CallResultService(_answer_extractor, _answer_store)
call_manager = CallManager()


class OutboundCallRequest(BaseModel):
    phone_number: str
    campaign_name: str = "twilio_outbound"
    lead_id: str | None = None
    business_name: str | None = None
    category: str | None = None
    notes: str | None = None


@router.post("/outbound")
async def start_outbound_call(request: OutboundCallRequest):
    """Trigger an outbound sales call.

    The realtime AI starts only after Twilio delivers media from the answered
    call, preventing idle AI websocket timeouts before caller audio arrives.
    """
    session = call_manager.create_session(
        campaign_name=request.campaign_name,
        phone_number=request.phone_number,
        direction="twilio",
        metadata={
            "lead_id": request.lead_id,
            "business_name": request.business_name,
            "category": request.category,
            "notes": request.notes,
            "phone_number": request.phone_number,
        },
    )
    call_status_tracker.upsert(
        session.call_id,
        "created",
        lead_id=request.lead_id,
        business_name=request.business_name,
        phone_number=request.phone_number,
        category=request.category,
    )
    bridge = AudioBridge(session, call_result_service)
    adapter = TwilioAdapter(audio_bridge=bridge)
    adapter.attach(session)

    try:
        logger.info(
            "dashboard_twilio_outbound_start",
            extra={"call_id": session.call_id, "lead_id": request.lead_id, "phone_number": request.phone_number},
        )
        await adapter.connect()
        call_status_tracker.upsert(session.call_id, "dialing", call_sid=adapter.call_sid)
    except Exception as exc:
        logger.exception(
            "dashboard_twilio_outbound_failed",
            extra={"call_id": session.call_id, "lead_id": request.lead_id, "phone_number": request.phone_number},
        )
        call_status_tracker.upsert(session.call_id, "failed", error=str(exc))
        await bridge.close_ai()
        call_manager.mark_failed(session, str(exc))
        call_manager.destroy_session(session.call_id)
        raise

    return {"call_id": session.call_id, "call_sid": adapter.call_sid, "status": "dialing"}


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
async def status_webhook(request: Request):
    """Acknowledge Twilio status callbacks without starting AI work inline."""
    try:
        form = await request.form()
        call_sid = form.get("CallSid")
        call_status = form.get("CallStatus")
        if call_sid or call_status:
            logger.info("twilio_status_callback", extra={"call_sid": call_sid, "call_status": call_status})
        if call_sid and call_status:
            _record_twilio_status(str(call_sid), str(call_status))
    except Exception:
        logger.exception("twilio_status_callback_parse_failed")
    return Response(status_code=200)


def _record_twilio_status(call_sid: str, call_status: str) -> None:
    adapter = TwilioAdapter._pending.get(call_sid)
    if adapter is None or adapter.session is None:
        return
    call_status_tracker.upsert(adapter.session.call_id, call_status, call_sid=call_sid)
    terminal_without_media = call_status in {"completed", "busy", "no-answer", "failed", "canceled"} and adapter.websocket is None
    if terminal_without_media:
        TwilioAdapter._pending.pop(call_sid, None)
        lead_id = adapter.session.metadata.get("lead_id")
        if lead_id:
            _answer_store.update_lead(lead_id, status=call_status, last_call_id=adapter.session.call_id, last_call_sid=call_sid)
        call_manager.destroy_session(adapter.session.call_id)



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
    adapter.session.metadata["call_sid"] = call_sid
    call_status_tracker.upsert(adapter.session.call_id, "media_connected", call_sid=call_sid, stream_sid=stream_sid)
    call_manager.mark_connected(adapter.session)

    try:
        call_status_tracker.upsert(adapter.session.call_id, "media_stream_active")
        await adapter.start()
    except Exception as exc:
        logger.exception("twilio_media_stream_failed", extra={"call_id": adapter.session.call_id, "call_sid": call_sid})
        call_status_tracker.upsert(adapter.session.call_id, "failed", error=str(exc))
        raise
    finally:
        call_status_tracker.upsert(adapter.session.call_id, "finished")
        call_manager.destroy_session(adapter.session.call_id)
        logger.info("Twilio call %s finished", adapter.session.call_id)


def router_ws_url(request: Request) -> str:
    """Builds the wss:// URL Twilio should stream audio to, from PUBLIC_BASE_URL."""
    base = PUBLIC_BASE_URL
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    return f"{ws_base}/media-stream"
