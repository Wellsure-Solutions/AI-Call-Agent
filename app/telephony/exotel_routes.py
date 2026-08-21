from __future__ import annotations

"""Exotel webhooks and media socket.

Mirrors `twilio_routes` minus TwiML, which Exotel has no equivalent of: the
stream URL is handed over at dial time instead, so by the time anything here
runs the CallSid is already bound.

Two things differ structurally and both are security-relevant:

  * Exotel does not sign its callbacks. `/exotel/status/{call_id}` is
    authenticated by an HMAC query token we minted, with an optional IP
    allowlist on top.

  * The media socket is a *separate endpoint* from Twilio's rather than one
    parser branching on message shape. Exotel says `stream_sid` where Twilio
    says `streamSid`, and the correlation logic that key feeds is what decides
    whose audio this is -- a shared parser guessing between them is not a
    guess worth making.
"""

import asyncio
import json
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response

from app.core.settings import (
    BARGE_IN_VOICE_ENERGY_THRESHOLD,
    EXOTEL_CALLBACK_ALLOWED_IPS,
    MEDIA_DUMP_DIR,
    METRICS_ENABLED,
    METRICS_FLUSH_SECONDS,
    METRICS_SILENCE_GAP_MS,
)
from app.integrations.deepgram.config import cached_greeting_audio
from app.services.call_service import CallResultService
from app.storage.sqlite_store import SQLiteCallStore
from app.telephony.adapters.exotel_adapter import ExotelAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.audio.media_dump import MediaDump
from app.telephony.call_session import CallSession
from app.telephony.callback_urls import valid_exotel_callback_token, valid_exotel_stream_token
from app.telephony.metrics import CallMetrics, MetricsWriter
from app.telephony.providers.exotel_provider import normalize_status
from app.telephony.state_machine import CallState
from app.telephony.twilio_routes import _record_signature_failure

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/exotel", tags=["exotel"])

_store: SQLiteCallStore | None = None
_result_service: CallResultService | None = None


def configure(store: SQLiteCallStore, result_service: CallResultService) -> None:
    global _store, _result_service
    _store, _result_service = store, result_service


def _repo() -> SQLiteCallStore:
    if _store is None:
        raise RuntimeError("Exotel repository is not configured")
    return _store


def _client_allowed(request: Request) -> bool:
    """Optional IP allowlist. Empty means disabled.

    Exotel publishes its egress ranges only on request, so this cannot be a
    default. It is defence in depth behind the HMAC token, never instead of
    it.
    """
    if not EXOTEL_CALLBACK_ALLOWED_IPS:
        return True
    client = request.client.host if request.client else ""
    return client in EXOTEL_CALLBACK_ALLOWED_IPS


def _valid_callback(call_id: str, request: Request) -> bool:
    """Exotel signs nothing, so the token we minted at dial time is the proof.

    Same constant-time comparison and same expiry discipline as the media
    token; only the payload and lifetime differ.
    """
    try:
        expiry = int(request.query_params.get("expiry") or 0)
    except (TypeError, ValueError):
        expiry = 0
    token = str(request.query_params.get("token") or "")
    return _client_allowed(request) and valid_exotel_callback_token(call_id, expiry, token)


@router.post("/status/{call_id}")
async def status_webhook(call_id: str, request: Request):
    """Exotel's call status callback.

    The status word is normalized by the provider before it reaches the store,
    so `PROVIDER_TERMINAL` keeps meaning exactly what it means for Twilio, and
    the store's existing rule that a nonterminal callback cannot regress a
    terminal one applies unchanged.
    """
    if not _valid_callback(call_id, request):
        _record_signature_failure("exotel_status", call_id)
        raise HTTPException(403, "Invalid Exotel callback token")

    form = dict(await request.form()) if _is_form(request) else {}
    if not form:
        try:
            form = dict(await request.json())
        except Exception:
            form = {}

    sid = str(form.get("CallSid") or form.get("callsid") or "")
    raw_status = form.get("Status") or form.get("status") or form.get("CallStatus") or ""
    if sid and raw_status:
        await _repo().aprovider_status(call_id, normalize_status(raw_status), sid)
    return Response(status_code=200)


def _is_form(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    return "form-urlencoded" in content_type or "multipart/form-data" in content_type


@router.websocket("/media-stream")
async def exotel_media_stream(websocket: WebSocket):
    """Exotel's bidirectional Voicebot stream.

    Correlation is deliberately in two halves, and both must pass:

      1. The HMAC token in the query string proves this URL came from us and
         has not expired. It covers `call_id` and `expiry` but *not* the
         CallSid, because the URL had to be built before the dial request was
         sent and no SID existed yet.

      2. The database proves the stream belongs to this call. The `call_sid`
         Exotel reports in its start event is checked against the SID bound to
         this `call_id` at dial time, and `claim_media` then re-checks it
         inside a conditional UPDATE that also enforces single ownership and
         refuses a terminal call.

    Do not collapse these into one. The token alone would let a replayed URL
    bind a different call's audio; the database check alone would accept a
    URL nobody signed. Equally, do not "fix" the token by adding the SID to
    it -- it is not knowable when the token is minted.
    """
    await websocket.accept()
    try:
        start = None
        for _ in range(3):
            msg = json.loads(await asyncio.wait_for(websocket.receive_text(), 5))
            if msg.get("event") == "start":
                start = msg.get("start") or {}
                break
        if not start:
            raise ValueError("missing start event")

        # Exotel echoes the stream URL's query string back as custom
        # parameters. Read them from inside the stream rather than from the
        # handshake URL, so what is validated is what the carrier actually
        # believes it is streaming.
        params = start.get("custom_parameters") or {}
        call_id = str(params.get("call_id") or "")
        sid = str(start.get("call_sid") or "")
        try:
            expiry = int(params.get("expiry") or 0)
        except (TypeError, ValueError):
            expiry = 0
        if not valid_exotel_stream_token(call_id, expiry, str(params.get("token") or "")):
            raise ValueError("invalid stream token")

        # Half two of the correlation: the SID must already be bound to this
        # call, by the dial that created the stream URL in the first place.
        known = await _repo().aget_call(call_id)
        if not known or not sid or known.get("call_sid") != sid:
            raise ValueError("stream does not match the call's bound provider SID")
        if known.get("provider") != "exotel":
            raise ValueError("call was not placed on Exotel")

        call = await _repo().aclaim_media(call_id, sid, str(uuid4()))
        if not call:
            raise ValueError("terminal, unknown, or already-owned call")
    except Exception:
        await websocket.close(code=1008)
        return

    session = CallSession(
        call_id=call_id,
        campaign_name="exotel_outbound",
        phone_number=call["phone_number"],
        # Keyed on the call's persisted provider, which is what selects the
        # mu-law audio profile for this leg.
        direction="exotel",
        metadata={
            "lead_id": call.get("lead_id"),
            "business_name": call.get("business_name"),
            "category": call.get("category"),
            "notes": call.get("notes"),
            "phone_number": call.get("phone_number"),
            "call_sid": sid,
            "media_connected": True,
        },
    )
    session.safe_transition_to(CallState.CONNECTING)
    session.safe_transition_to(CallState.CONNECTED)

    greeting = cached_greeting_audio(session.metadata)
    metrics, writer = _build_metrics(call_id, "cached" if greeting else "provider")
    dump = MediaDump.create(MEDIA_DUMP_DIR, call_id)
    if metrics is not None:
        metrics.bind()

    bridge = AudioBridge(
        session, _result_service, hard_interrupt=False, metrics=metrics,
        greeting_already_played=bool(greeting),
    )
    adapter = ExotelAdapter(bridge, metrics=metrics, media_dump=dump)
    adapter.pending_greeting = greeting
    adapter.attach(session)
    adapter.call_sid = sid
    adapter.stream_sid = start.get("stream_sid")
    adapter.websocket = websocket

    close_reason = "completed"
    try:
        await adapter.start()
    except Exception:
        close_reason = "error"
        raise
    finally:
        if session.ended_at is None:
            await bridge.stop("completed")
        if dump is not None:
            dump.close()
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
