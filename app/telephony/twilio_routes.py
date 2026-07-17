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

FIXED: /twilio/status previously only reacted to "answered"/"in-progress".
For calls that were never answered (no-answer/busy/failed/canceled) or that
ended without the media-stream websocket ever connecting, nothing ever
cleaned up the pre-warmed Deepgram session or advanced the call/lead status.
That left the call permanently stuck at "dialing"/"ai_ready", which in turn
stalled the batch runner in main.py (BATCH_CONCURRENCY_LIMIT=1 never sees
that lead leave the "calling" state, so it never starts the next lead).

This version adds explicit handling for every terminal Twilio call status.
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

# Twilio's actual CallStatus values for a call that will never open a media
# stream. These intentionally match BATCH_TERMINAL_STATUSES in main.py so the
# batch runner's polling loop recognizes them immediately.
TERMINAL_CALL_STATUSES = {"completed", "failed", "busy", "no-answer", "canceled"}


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

    The realtime AI is pre-warmed (Deepgram connection opened) before Twilio
    confirms pickup, so the greeting is ready the instant the media stream
    connects. If the call is never answered, /twilio/status is responsible
    for tearing this pre-warmed connection back down -- see
    _cleanup_unanswered_call below.
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
        # Pre-warm the realtime AI connection while Twilio is still dialing so
        # the greeting audio is already queued when the recipient answers and
        # Twilio opens the media stream. This removes the several-second
        # answer-to-first-audio delay caused by starting Deepgram only after
        # pickup.
        await bridge.start()
        call_status_tracker.upsert(session.call_id, "ai_ready")
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
    """Acknowledge Twilio status callbacks without starting AI work inline.

    Handles two disjoint cases:
      1. Call was answered / is in-progress -> warm the AI connection if it
         somehow wasn't already (see _warm_answered_call).
      2. Call reached a terminal status without ever opening the media
         stream (no-answer, busy, failed, canceled) -- or ended in a way
         that the media_stream handler never got to run its own cleanup for
         (completed) -- -> tear down the pre-warmed Deepgram session,
         mark the call/lead status, and free call_manager resources.

    Both branches are fire-and-forget background tasks so this webhook
    always returns 200 quickly, matching Twilio's short response budget.
    """
    try:
        form = await request.form()
        call_sid = form.get("CallSid")
        call_status = form.get("CallStatus")
        if call_sid or call_status:
            logger.info("twilio_status_callback", extra={"call_sid": call_sid, "call_status": call_status})

        if call_sid and call_status in {"answered", "in-progress"}:
            asyncio.create_task(_warm_answered_call(str(call_sid)))

        elif call_sid and call_status in TERMINAL_CALL_STATUSES:
            asyncio.create_task(_cleanup_unanswered_call(str(call_sid), str(call_status)))

    except Exception:
        logger.exception("twilio_status_callback_parse_failed")
    return Response(status_code=200)


async def _warm_answered_call(call_sid: str) -> None:
    adapter = TwilioAdapter._pending.get(call_sid)
    if adapter is None or adapter.audio_bridge is None or adapter.session is None:
        return
    try:
        # Idempotent -- AudioBridge.start() no-ops if the connection was
        # already pre-warmed and is still alive. If it died and reconnects
        # here, that's a fresh, correctly-kept-alive connection either way.
        await adapter.audio_bridge.start()
        call_status_tracker.upsert(adapter.session.call_id, "ai_ready", call_sid=call_sid)
    except Exception as exc:
        logger.exception("answered_call_ai_warm_failed", extra={"call_sid": call_sid})
        call_status_tracker.upsert(adapter.session.call_id, "ai_warm_failed", error=str(exc), call_sid=call_sid)


async def _cleanup_unanswered_call(call_sid: str, call_status: str) -> None:
    """Tear down a call that reached a terminal Twilio status without the
    media_stream websocket handler ever running (so its own cleanup path --
    the `finally` block in media_stream() -- never fired).

    This is the fix for the pipeline-wide stall: without this, an
    unanswered call leaves TwilioAdapter._pending populated and
    call_status_tracker frozen at "dialing"/"ai_ready" forever, which keeps
    the lead's status at "calling" in the batch runner's eyes and blocks
    BATCH_CONCURRENCY_LIMIT from ever admitting the next queued lead.
    """
    adapter = TwilioAdapter._pending.pop(call_sid, None)
    if adapter is None or adapter.session is None:
        # media_stream() already ran (call connected then ended normally)
        # and handled its own cleanup -- nothing left to do here.
        logger.info("cleanup_unanswered_call_noop_already_handled", extra={"call_sid": call_sid, "call_status": call_status})
        return

    try:
        if adapter.audio_bridge is not None:
            await adapter.audio_bridge.close_ai()
    except Exception:
        logger.exception("cleanup_unanswered_call_close_ai_failed", extra={"call_sid": call_sid})

    call_status_tracker.upsert(adapter.session.call_id, call_status, call_sid=call_sid)

    lead_id = adapter.session.metadata.get("lead_id")
    if lead_id:
        _answer_store.update_lead(
            lead_id,
            status="call_failed" if call_status != "completed" else "call_failed",
            last_error=f"call ended before media stream connected: {call_status}",
        )

    try:
        call_manager.destroy_session(adapter.session.call_id)
    except Exception:
        logger.exception("cleanup_unanswered_call_destroy_session_failed", extra={"call_sid": call_sid})

    logger.info(
        "unanswered_call_cleaned_up",
        extra={"call_sid": call_sid, "call_status": call_status, "call_id": adapter.session.call_id},
    )


@media_router.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()

    # Twilio's first message is "connected"; the second is "start", which
    # carries the callSid and streamSid needed to bind audio to this socket.
    call_sid: str | None = None
    stream_sid: str | None = None
    try:
        while call_sid is None:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=5)
            msg = json.loads(raw)
            if msg.get("event") == "start":
                call_sid = msg["start"]["callSid"]
                stream_sid = msg["start"]["streamSid"]
    except asyncio.TimeoutError:
        logger.warning("media_stream_start_event_timeout")
        await websocket.close()
        return

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