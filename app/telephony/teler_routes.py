from __future__ import annotations

"""Teler webhooks, call flow, and media socket.

Structurally this mirrors `twilio_routes` rather than `exotel_routes`, because
Teler's call model is Twilio's: the dial names a `flow_url`, Teler POSTs to it
once the call connects, and the response -- Teler's analogue of TwiML -- is
what names the media socket. That ordering is what lets the stream token cover
the carrier's own call id, which Exotel's cannot.

Three things differ from Twilio and all three are security-relevant:

  * **The flow endpoint is authenticated by us.** FreJun document a signature
    on webhooks; they document nothing about the flow request. An
    unauthenticated flow endpoint hands out a media URL -- and with it a live
    stream token -- to anyone who guesses a call_id, so it carries an HMAC
    query token minted at dial time.

  * **Status callbacks are checked twice, deliberately.** Teler signs them
    (`X-Teler-Signature`, HMAC-SHA256 over "{timestamp}.{raw_body}") but with
    a secret set per Voice App in FreJun's dashboard. This service cannot
    confirm that secret is present or correct until a live call fails, and
    both failure modes are silent: reject everything, or authenticate nothing.
    So our own token is the control that must pass, and the signature is
    verified on top of it when TELER_WEBHOOK_SECRET is configured -- the same
    posture as Exotel's optional IP allowlist, defence in depth rather than
    instead of.

  * **The media socket is its own endpoint.** Teler says
    `{"type": "audio", "data": {"audio_b64": ...}}` where Twilio says
    `{"event": "media", "media": {"payload": ...}}`, and the correlation logic
    those keys feed is what decides whose audio this is. A shared parser
    guessing between them is not a guess worth making.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from app.core.settings import (
    BARGE_IN_VOICE_ENERGY_THRESHOLD,
    MAX_CALL_SECONDS,
    MEDIA_DUMP_DIR,
    METRICS_ENABLED,
    METRICS_FLUSH_SECONDS,
    METRICS_SILENCE_GAP_MS,
    RING_TIMEOUT_SECONDS,
    TELER_CHUNK_MS,
    TELER_INCOMING_MEDIA_URL,
    TELER_INCOMING_SECRET,
    TELER_RECORD,
    TELER_WEBHOOK_SECRET,
)
from app.integrations.deepgram.config import cached_greeting_audio
from app.services.call_service import CallResultService
from app.storage.sqlite_store import SQLiteCallStore
from app.telephony.adapters.teler_adapter import TelerAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.audio.media_dump import MediaDump
from app.telephony.call_session import CallSession
from app.telephony.callback_urls import (
    teler_media_stream_url,
    valid_teler_callback_token,
    valid_teler_flow_token,
    valid_teler_stream_token,
)
from app.telephony.metrics import CallMetrics, MetricsWriter
from app.telephony.providers.teler_provider import TelerProvider, normalize_event
from app.telephony.state_machine import CallState
from app.telephony.twilio_routes import _record_signature_failure

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/teler", tags=["teler"])

_store: SQLiteCallStore | None = None
_result_service: CallResultService | None = None


def configure(store: SQLiteCallStore, result_service: CallResultService) -> None:
    global _store, _result_service
    _store, _result_service = store, result_service


def _repo() -> SQLiteCallStore:
    if _store is None:
        raise RuntimeError("Teler repository is not configured")
    return _store


# ===========================================================================
# TEMPORARY DEBUG INSTRUMENTATION -- remove once the wire format is confirmed.
#
# Everything between this banner and the matching one below exists to capture
# one real Teler call, because the adapter's message format was pinned from
# FreJun's published sources rather than from a packet capture (see
# docs/teler-adapter.md §5). Delete this block and its three call sites in
# `teler_media_stream` when that is done.
#
# Two deliberate departures from this codebase's logging style, both required
# for the output to exist at all:
#
#   * `warning`, not `info`. Nothing in this repo configures logging, so under
#     uvicorn's default config the root logger has no handler and app loggers
#     fall through to `logging.lastResort` -- a stderr handler fixed at
#     WARNING. Every `logger.info(...)` in this package is currently invisible
#     in production. That is worth fixing properly with a logging config; it
#     is not worth discovering during the one test call this exists for.
#
#   * The payload is inline in the message, not in `extra={...}`. The same
#     lastResort handler formats with `%(message)s`, so `extra` fields are
#     dropped entirely. An `extra`-based version of this logs the event name
#     and none of the data.
# ===========================================================================

# Enough of a message to see its structure and key names; a 400ms audio chunk
# base64s to ~10KB and there is nothing to learn from the tail of it. The full
# length is always reported, so truncation is never silent.
_RAW_LOG_LIMIT = 2000


def _debug_handshake(call_id: str, expiry: int, token: str) -> None:
    """The query string Teler connected with, minus the token's value.

    The token is a live media HMAC; its length and presence answer "did Teler
    send one back", which is the diagnostic question, and its value would put
    a working credential in the log. Whether the expiry has already passed is
    reported because a stream token outliving ring time is one of the two most
    likely causes of a 1008 here.
    """
    now = int(time.time())
    logger.warning(
        "TELER_DEBUG handshake call_id=%r expiry=%s now=%s expired=%s "
        "token_present=%s token_len=%s",
        call_id, expiry, now, expiry < now, bool(token), len(token),
    )


def _debug_raw(attempt: int, raw: str) -> None:
    """One message exactly as it arrived, before json.loads or any check."""
    body = raw if len(raw) <= _RAW_LOG_LIMIT else raw[:_RAW_LOG_LIMIT] + "...<truncated>"
    logger.warning(
        "TELER_DEBUG inbound attempt=%s len=%s raw=%s", attempt, len(raw), body
    )


def _debug_status(call_id: str, raw_body: bytes, headers: Any) -> None:
    """Teler's status webhook, raw, plus the headers that identify the pin.

    Logged *before* the token check on purpose: a callback we reject is
    exactly the one worth seeing, and right now this is the only Teler traffic
    reaching the server at all, so it is the only evidence available about
    what Teler thinks is happening to the call.

    `X-Teler-Api-Version` is present only on 2026-06-01, so its absence
    confirms the pin without anyone reading the dashboard -- and the event
    name and the `call_id` format in the body say whether the identifier split
    between Teler's versioned and unversioned surfaces is real.
    """
    body = raw_body.decode("utf-8", "replace")
    if len(body) > _RAW_LOG_LIMIT:
        body = body[:_RAW_LOG_LIMIT] + "...<truncated>"
    logger.warning(
        "TELER_DEBUG status call_id=%r api_version=%r source=%r event_id=%r len=%s raw=%s",
        call_id,
        headers.get("x-teler-api-version"),
        headers.get("x-teler-source"),
        headers.get("x-teler-event-id"),
        len(raw_body), body,
    )


def _debug_incoming(raw_body: bytes) -> None:
    """An inbound flow or status request, raw, before parsing.

    Inbound is brand new and has never been seen on a live call, so its body
    shape is inferred from the outbound flow's -- FreJun document one schema
    for both directions, distinguished only by `direction`. This is what
    confirms or corrects that on the first real callback.
    """
    body = raw_body.decode("utf-8", "replace")
    if len(body) > _RAW_LOG_LIMIT:
        body = body[:_RAW_LOG_LIMIT] + "...<truncated>"
    logger.warning("TELER_DEBUG incoming len=%s raw=%s", len(raw_body), body)


def _debug_flow(call_id: str, raw_body: bytes) -> None:
    """Teler's flow POST exactly as it arrived, before any parsing.

    Added after the media-socket logging, because if the flow request is what
    fails then the media socket is never opened and the instrumentation there
    produces nothing at all -- an empty log that looks like the call never
    happened.
    """
    body = raw_body.decode("utf-8", "replace")
    if len(body) > _RAW_LOG_LIMIT:
        body = body[:_RAW_LOG_LIMIT] + "...<truncated>"
    logger.warning("TELER_DEBUG flow call_id=%r len=%s raw=%s", call_id, len(raw_body), body)


def _debug_flow_correlation(call_id: str, teler_call_id: str, call: dict | None, bound: bool | None) -> None:
    """Whether Teler's identifier for the call matches the one we bound.

    This is the single comparison the flow route turns on, and the one most
    likely to fail: FreJun's call-flows reference says "the call_id and
    account_id formats depend on the webhook version pinned on the owning
    Voice App", and their versioning reference says 2025-08-01 uses raw UUIDs
    where 2026-06-01 uses cs_-prefixed ids. The REST API that returns the id
    we bind at dial time is *not* versioned. So on a Voice App pinned to
    2025-08-01, these two are the same call under two different names, and
    bind_call_sid refuses the second one.
    """
    stored = (call or {}).get("call_sid")
    logger.warning(
        "TELER_DEBUG flow_correlation call_id=%r teler_call_id=%r stored_call_sid=%r "
        "match=%s row_found=%s provider=%r bound=%s",
        call_id, teler_call_id, stored, stored == teler_call_id,
        call is not None, (call or {}).get("provider"), bound,
    )


def _debug_correlation_failure(call_id: str, exc: BaseException) -> None:
    """Why the socket was closed 1008, instead of swallowing it silently.

    The five correlation failures each raise a distinct ValueError message, so
    the exception text alone says which half of the check rejected the stream.
    A timeout or disconnect from `receive_text` lands here too.
    """
    logger.warning(
        "TELER_DEBUG correlation_failed call_id=%r %s: %s",
        call_id, type(exc).__name__, exc,
        exc_info=exc,
    )


# =========================== END TEMPORARY DEBUG ===========================


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def _query_token(request: Request) -> tuple[int, str]:
    try:
        expiry = int(request.query_params.get("expiry") or 0)
    except (TypeError, ValueError):
        expiry = 0
    return expiry, str(request.query_params.get("token") or "")


def _valid_teler_signature(raw_body: bytes, headers: Any) -> bool:
    """Verify `X-Teler-Signature` over "{timestamp}.{raw_body}".

    Only ever consulted as an *additional* check, never as the only one, so a
    False here can safely be strict.

    FreJun publish the algorithm and the signed string but not the encoding of
    the digest, so hex and base64 are both accepted, with an optional
    `sha256=` prefix stripped. That is not a weakening: every candidate is
    compared in constant time against a digest of the same secret, and an
    attacker without the secret cannot produce any of the forms. Guessing a
    single encoding instead would reject every callback on a wrong guess --
    exactly the silent total outage this module's docstring is about.

    Freshness is not enforced from `X-Teler-Timestamp`, whose units are not
    documented; the callback token's own expiry already bounds the replay
    window, and treating milliseconds as seconds would reject everything.
    """
    if not TELER_WEBHOOK_SECRET:
        return False
    provided = str(headers.get("x-teler-signature") or "")
    timestamp = str(headers.get("x-teler-timestamp") or "")
    if not provided or not timestamp:
        return False
    if provided.startswith("sha256="):
        provided = provided[len("sha256="):]
    digest = hmac.new(
        TELER_WEBHOOK_SECRET.encode(),
        timestamp.encode() + b"." + raw_body,
        hashlib.sha256,
    ).digest()
    candidates = (digest.hex(), base64.b64encode(digest).decode("ascii"))
    return any(hmac.compare_digest(candidate, provided) for candidate in candidates)


def _valid_callback(call_id: str, request: Request, raw_body: bytes) -> bool:
    """Our token must pass; Teler's signature must pass too when we can check it."""
    expiry, token = _query_token(request)
    if not valid_teler_callback_token(call_id, expiry, token):
        return False
    if TELER_WEBHOOK_SECRET and not _valid_teler_signature(raw_body, request.headers):
        logger.warning("teler_signature_rejected", extra={"call_id": call_id})
        return False
    return True


# ---------------------------------------------------------------------------
# Call flow -- Teler's TwiML equivalent
# ---------------------------------------------------------------------------
@router.post("/flow/{call_id}")
async def flow_webhook(call_id: str, request: Request):
    """Return the stream flow that binds this call to our media socket.

    Teler POSTs `{call_id, account_id, from_number, to_number, direction}` here
    once the call connects, where `call_id` is *Teler's* identifier for the
    call. That is the first moment the carrier's id and ours are both known, so
    it is where the media URL and its token are minted -- the same point in the
    call's life as `/twilio/twiml`, for the same reason.

    The SID is re-bound rather than merely compared. `bind_call_sid` accepts a
    row whose `call_sid` is NULL *or* already equal, so this is idempotent when
    the coordinator has already bound the id returned by the dial, and a
    genuine mismatch -- a flow request naming a different call -- fails
    correlation instead of handing out a token for somebody else's stream.
    """
    if not valid_teler_flow_token(call_id, *_query_token(request)):
        _record_signature_failure("teler_flow", call_id)
        raise HTTPException(403, "Invalid Teler flow token")

    # `_json_body(request)` is exactly these two lines; split so the TEMPORARY
    # logging can see the body before it is parsed.
    raw_body = await request.body()
    _debug_flow(call_id, raw_body)
    body = _decode_json(raw_body)
    teler_call_id = str(body.get("call_id") or "")
    call = await _repo().aget_call(call_id)
    _debug_flow_correlation(call_id, teler_call_id, call, None)
    if not call or not teler_call_id:
        raise HTTPException(409, "Call correlation failed")
    if call.get("provider") != "teler":
        raise HTTPException(409, "Call was not placed on Teler")
    bound = await asyncio.to_thread(
        _repo().bind_call_sid, call_id, teler_call_id, RING_TIMEOUT_SECONDS, MAX_CALL_SECONDS
    )
    _debug_flow_correlation(call_id, teler_call_id, call, bound)
    if not bound:
        raise HTTPException(409, "Call correlation failed")

    flow = TelerProvider.build_stream_flow(
        ws_url=teler_media_stream_url(call_id, teler_call_id),
        chunk_ms=TELER_CHUNK_MS,
        record=TELER_RECORD,
    )
    return JSONResponse(flow)


# ---------------------------------------------------------------------------
# Inbound calls -- somebody ringing the virtual number back
# ---------------------------------------------------------------------------
def _valid_incoming_secret(request: Request) -> bool:
    """Authenticate FreJun's Incoming Call URL by a single shared secret.

    This cannot be the per-call HMAC the outbound flow uses. That URL is minted
    at dial time against a call that already exists; the Incoming Call URL is
    typed into the Voice App once, before any inbound call exists, so there is
    nothing per-call to sign.

    Fails closed when the secret is unset. An unauthenticated endpoint here is
    a public write into the callbacks table -- anyone who guessed the path could
    fill the operator's follow-up list with numbers that never rang.
    """
    if not TELER_INCOMING_SECRET:
        return False
    return hmac.compare_digest(str(request.query_params.get("key") or ""), TELER_INCOMING_SECRET)


@router.post("/incoming")
async def incoming_call_flow(request: Request):
    """FreJun's Incoming Call URL: record the callback, then end the call.

    Set this as the Voice App's Incoming Call URL. Teler POSTs the same body
    shape as the outbound flow -- `{call_id, account_id, from_number,
    to_number, direction}` -- and expects one call-flow action back.

    Recording happens before the response is built and never fails the request:
    the whole point is to capture that this number rang, and returning a 5xx
    would make Teler retry, which would either duplicate the callback or leave
    the caller listening to silence while we retried a database write.

    The agent is deliberately not run on inbound. Answering with the outbound
    pitch to somebody who rang *us* is the wrong conversation, and the flow
    here exists to log the callback for a human to return.
    """
    if not _valid_incoming_secret(request):
        _record_signature_failure("teler_incoming", "inbound")
        raise HTTPException(403, "Invalid Teler incoming key")

    raw_body = await request.body()
    _debug_incoming(raw_body)
    body = _decode_json(raw_body)
    from_number = str(body.get("from_number") or "")
    try:
        recorded = await _repo().arecord_inbound_call(
            from_number=from_number,
            to_number=str(body.get("to_number") or ""),
            provider="teler",
            provider_call_id=str(body.get("call_id") or ""),
        )
        logger.warning(
            "TELER_DEBUG inbound_recorded id=%r from=%r matched_lead=%r",
            recorded.get("inbound_id"), recorded.get("from_number"), recorded.get("lead_id"),
        )
    except Exception:
        logger.exception("inbound_call_not_recorded", extra={"from_number": from_number})

    return JSONResponse(TelerProvider.build_incoming_flow(TELER_INCOMING_MEDIA_URL))


@router.post("/incoming/status")
async def incoming_status_webhook(request: Request):
    """The Voice App's Call status URL, for inbound calls.

    Separate from `/teler/status/{call_id}`: that one is per-call and carries a
    token minted for an outbound call we placed. This one is configured once on
    the Voice App, so it is authenticated by the same shared secret as the
    Incoming Call URL and correlates on Teler's own call id.

    Only ever updates an existing callback row -- see `update_inbound_status`.
    Outbound calls report to their own per-call URL, so anything arriving here
    that we cannot match is acknowledged and dropped rather than invented.
    """
    if not _valid_incoming_secret(request):
        _record_signature_failure("teler_incoming_status", "inbound")
        raise HTTPException(403, "Invalid Teler incoming key")

    raw_body = await request.body()
    _debug_incoming(raw_body)
    payload = _decode_json(raw_body)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    event = payload.get("type") or payload.get("event")
    provider_call_id = str(payload.get("call_id") or data.get("call_id") or "")
    status = normalize_event(event, data.get("reason"))
    if provider_call_id and status:
        duration = data.get("duration_seconds")
        if duration is None:
            duration = data.get("duration")
        try:
            duration = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        await _repo().aupdate_inbound_status(provider_call_id, status, duration)
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Status webhook
# ---------------------------------------------------------------------------
@router.post("/status/{call_id}")
async def status_webhook(call_id: str, request: Request):
    """Teler's call status webhook, on either published payload version.

    The status word is normalized by the provider before it reaches the store,
    so `PROVIDER_TERMINAL` keeps meaning exactly what it means for Twilio, and
    the store's rule that a nonterminal callback cannot regress a terminal one
    applies unchanged.

    Non-call events are acknowledged and ignored. Teler delivers
    `stream.initiated`, `stream.completed`, `recording.completed` and
    `recording.failed` to this same URL, and its own documentation warns that
    `stream.completed` does not imply `call.completed` -- a stream can end
    while the call is still up. Mapping one of those onto a call status would
    terminalize a live call: the customer is hung up on mid-sentence and their
    number is released for a redial. `normalize_event` returning None is what
    prevents that, so the guard below must stay.
    """
    raw_body = await request.body()
    _debug_status(call_id, raw_body, request.headers)
    if not _valid_callback(call_id, request, raw_body):
        _record_signature_failure("teler_status", call_id)
        raise HTTPException(403, "Invalid Teler callback token")

    payload = _decode_json(raw_body)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    # `type` on 2026-06-01, `event` on 2025-08-01. Both versions are read
    # rather than one being assumed, because the version is pinned per Voice
    # App in FreJun's dashboard and can be changed there without a deploy.
    event = payload.get("type") or payload.get("event")
    # Root `call_id` exists only on 2026-06-01; `data.call_id` on both.
    sid = str(payload.get("call_id") or data.get("call_id") or "")
    status = normalize_event(event, data.get("reason"))
    if sid and status:
        await _repo().aprovider_status(call_id, status, sid)
    return Response(status_code=200)


async def _json_body(request: Request) -> dict[str, Any]:
    return _decode_json(await request.body())


def _decode_json(raw: bytes) -> dict[str, Any]:
    """Parse a JSON object body, tolerating anything else.

    A malformed body must not 500: FastAPI would turn that into a 5xx and Teler
    would redeliver it up to eight times, and the call it refers to is not
    helped by any of them.
    """
    try:
        payload = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# Media socket
# ---------------------------------------------------------------------------
@router.websocket("/media-stream")
async def teler_media_stream(websocket: WebSocket):
    """Teler's bidirectional media stream.

    Correlation is in two halves and both must pass, exactly as on Exotel:

      1. The HMAC token in the query string proves this URL came from us and
         has not expired. Unlike Exotel's it *does* cover the carrier's call
         id, because the flow route that minted it already knew one.

      2. The database proves the stream belongs to this call. The `call_id`
         Teler reports in its start message is checked against the id bound to
         this call, and `claim_media` then re-checks it inside a conditional
         UPDATE that also enforces single ownership and refuses a terminal
         call.

    Do not collapse these into one. The token alone would let a replayed URL
    bind a different call's audio; the database check alone would accept a URL
    nobody signed.

    The query string is read from the handshake rather than from inside the
    stream, which is where it differs from Exotel: Teler's start message
    carries no echo of the URL's parameters.

    What the start message says about *which call this is* is deliberately not
    used. Teler names the same call differently on this socket than on every
    other surface -- see the comment on the `known` lookup below -- so
    comparing them rejects every stream. The correlation is therefore Exotel's
    shape, not Twilio's: the token proves the URL is ours, unexpired and
    minted against the id we bound, and the database proves ownership. Both
    still required, neither sufficient.
    """
    await websocket.accept()
    # Bound before the try so the TEMPORARY failure logging below cannot raise
    # UnboundLocalError and skip the close(1008). Reassigned immediately.
    call_id = ""
    try:
        call_id = str(websocket.query_params.get("call_id") or "")
        try:
            expiry = int(websocket.query_params.get("expiry") or 0)
        except (TypeError, ValueError):
            expiry = 0
        token = str(websocket.query_params.get("token") or "")

        _debug_handshake(call_id, expiry, token)

        # Cheap rejection before touching the database, so an unsigned or
        # long-expired URL costs nothing to refuse.
        if not token or expiry < int(time.time()):
            raise ValueError("missing or expired stream token")

        start = None
        for attempt in range(3):
            raw = await asyncio.wait_for(websocket.receive_text(), 5)
            _debug_raw(attempt, raw)
            msg = json.loads(raw)
            if msg.get("type") == "start":
                start = msg
                break
        if not start:
            raise ValueError("missing start message")

        # The carrier id comes from the call row, never from the start
        # message, because Teler names the same call differently here than
        # anywhere else. Confirmed on a real call: the dial response, the flow
        # request and every status webhook agreed on
        # `1b3faa9c-4383-4d87-9571-bd51751950cb`, while the start message on
        # the media socket announced `cs_5NGFF35W81ACMAH2PEVCQVCQ93`. Those are
        # two identifier spaces for one call and there is no local mapping
        # between them -- FreJun's versioning reference says the media-
        # streaming protocol is "unaffected by this pin", so it is on the
        # prefixed scheme whatever the Voice App's webhook version is.
        known = await _repo().aget_call(call_id)
        sid = str((known or {}).get("call_sid") or "")
        if not known or not sid:
            raise ValueError("unknown or unbound call")

        # The token still proves everything it proved before: that this URL was
        # minted by us, for this call_id, against the carrier id the flow route
        # bound, and that it has not expired. What changed is only that the sid
        # it is checked against comes from our own row instead of from the
        # peer. That is not a weakening -- the peer supplied that value, so it
        # could always choose which pair to be checked against, and it still
        # cannot produce a token for any pair without STREAM_SECRET.
        if not valid_teler_stream_token(call_id, sid, expiry, token):
            raise ValueError("invalid stream token")
        if known.get("provider") != "teler":
            raise ValueError("call was not placed on Teler")

        # Half two is unchanged and still required: `claim_media` re-checks the
        # sid inside a conditional UPDATE that also enforces single ownership
        # and refuses a terminal call.
        call = await _repo().aclaim_media(call_id, sid, str(uuid4()))
        if not call:
            raise ValueError("terminal, unknown, or already-owned call")
    except Exception as exc:
        _debug_correlation_failure(call_id, exc)
        await websocket.close(code=1008)
        return

    session = CallSession(
        call_id=call_id,
        campaign_name="teler_outbound",
        phone_number=call["phone_number"],
        # Keyed on the call's persisted provider, which is what selects the
        # mu-law audio profile for this leg.
        direction="teler",
        metadata={
            "lead_id": call.get("lead_id"),
            "business_name": call.get("business_name"),
            "category": call.get("category"),
            "notes": call.get("notes"),
            "phone_number": call.get("phone_number"),
            # The REST/webhook id. This is the one every control-plane call
            # takes -- notably the hangup, which 404s on the media-plane id.
            "call_sid": sid,
            # The media plane's own name for the same call, recorded because
            # it is the only id that appears on this socket and so the only
            # one to quote when raising a stream problem with FreJun.
            "teler_media_call_id": str(start.get("call_id") or ""),
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
    adapter = TelerAdapter(bridge, metrics=metrics, media_dump=dump)
    adapter.pending_greeting = greeting
    adapter.attach(session)
    adapter.call_sid = sid
    # Teler's own name for the stream. The base class refuses to start without
    # one, and it is the only handle on this stream Teler ever gives us.
    adapter.stream_sid = str(start.get("stream_id") or sid)
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
