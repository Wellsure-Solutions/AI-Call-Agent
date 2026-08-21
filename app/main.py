import asyncio
import json
import logging
import base64
import hmac
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, Response

from app.integrations.deepgram.config import DEEPGRAM_API_KEY
from app.services.answer_extractor import AnswerExtractor
from app.services.call_service import CallResultService
from app.storage.sqlite_store import ActiveDataError, SQLiteCallStore, SuppressedError
from app.core.settings import ADMIN_PASSWORD, ADMIN_USERNAME, DATA_DIR, DATABASE_PATH, HOST, INDEX_HTML, MAX_CONCURRENT_CALLS, PORT, START_INTERVAL_SECONDS, EXTRACTION_MAX_ATTEMPTS, EXTRACTION_TIMEOUT_SECONDS, EXTRACTION_RETRY_DELAY_SECONDS, RING_TIMEOUT_SECONDS, MAX_CALL_SECONDS, RECONCILIATION_MAX_ATTEMPTS, ABANDONED_JOB_GRACE_SECONDS
from app.services.call_coordinator import DurableCallCoordinator
from app.telephony.adapters.browser_adapter import BrowserAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.call_manager import CallManager
from app.telephony.twilio_routes import OutboundCallRequest, configure as configure_twilio, media_router, router as twilio_router, signature_failure_health, start_outbound_call

logger = logging.getLogger(__name__)

answer_extractor = AnswerExtractor()
answer_store = SQLiteCallStore(DATABASE_PATH, DATA_DIR)
call_result_service = CallResultService(answer_extractor, answer_store, timeout=EXTRACTION_TIMEOUT_SECONDS, max_attempts=EXTRACTION_MAX_ATTEMPTS)
call_manager = CallManager()
configure_twilio(answer_store, call_result_service)
coordinator = DurableCallCoordinator(
    answer_store, MAX_CONCURRENT_CALLS, START_INTERVAL_SECONDS,
    ring_timeout=RING_TIMEOUT_SECONDS, max_call_seconds=MAX_CALL_SECONDS,
    extraction_timeout=EXTRACTION_TIMEOUT_SECONDS,
    extraction_retry_delay=EXTRACTION_RETRY_DELAY_SECONDS,
    extractor=answer_extractor,
    reconciliation_max_attempts=RECONCILIATION_MAX_ATTEMPTS,
    abandoned_grace_seconds=ABANDONED_JOB_GRACE_SECONDS,
)

def warn_about_unrendered_audio() -> list[str]:
    """Report pre-rendered phrases missing for the *current* voice settings.

    The cache key covers the voice, the model and the text, so changing any of
    them silently invalidates it. Nothing fails: the greeting falls back to
    live synthesis and the fallback goodbye is simply not spoken. Both are
    invisible from the outside -- a voice change once cost every call in a
    batch its closing line, and it was only found by reading a transcript.
    """
    from app.integrations.deepgram.config import cached_closing_audio, cached_greeting_audio

    missing = []
    if cached_greeting_audio() is None:
        missing.append("greeting (calls will wait on live synthesis, ~2s of dead air each)")
    if cached_closing_audio() is None:
        missing.append("closing (a call the model does not close will end in silence)")
    for item in missing:
        logger.warning(
            "prerendered_audio_missing",
            extra={"phrase": item, "remedy": "python scripts/prerender_greeting.py"},
        )
    return missing


def check_startup_configuration() -> list[str]:
    """Settings whose absence breaks calls without any error appearing.

    Every one of these has already cost a call. STREAM_SECRET is the reason
    this function exists: a settings rename left it reading empty,
    `valid_stream_token` rejects every media stream when the secret is blank,
    and each outbound call dropped two seconds after the customer answered --
    with a clean 200 on every webhook and nothing in the log. Browser calls
    carry no media token, so they kept working and hid it.

    Reported rather than raised. A server that refuses to boot is worse than
    one that boots and says loudly what will not work.
    """
    from app.core.settings import (
        DEEPGRAM_API_KEY,
        PUBLIC_BASE_URL,
        STREAM_SECRET,
        TWILIO_ACCOUNT_SID,
        TWILIO_AUTH_TOKEN,
        TWILIO_FROM_NUMBER,
    )

    problems = []
    if not STREAM_SECRET:
        problems.append(
            "STREAM_SECRET is empty -- every Twilio media stream will be rejected "
            "and every outbound call will drop seconds after being answered"
        )
    if not DEEPGRAM_API_KEY:
        problems.append("DEEPGRAM_API_KEY is not set -- the agent cannot speak or listen")
    if not TWILIO_AUTH_TOKEN:
        problems.append("TWILIO_AUTH_TOKEN is not set -- webhook signatures cannot be verified")
    for name, value in (
        ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
        ("TWILIO_FROM_NUMBER", TWILIO_FROM_NUMBER),
        ("PUBLIC_BASE_URL", PUBLIC_BASE_URL),
    ):
        if not value:
            problems.append(f"{name} is not set -- outbound calls cannot be placed")

    for problem in problems:
        logger.error("startup_configuration_problem", extra={"problem": problem})
    return problems


@asynccontextmanager
async def lifespan(_: FastAPI):
    for problem in check_startup_configuration():
        print(f"[startup] MISCONFIGURED: {problem}")
    for item in warn_about_unrendered_audio():
        print(f"[startup] NOT RENDERED for the current voice: {item}")
        print("[startup]   fix: python scripts/prerender_greeting.py")
    task=asyncio.create_task(coordinator.run())
    yield
    coordinator.stop(); await task

app = FastAPI(title="Autonomous Calling Agent", lifespan=lifespan)
app.include_router(twilio_router)
app.include_router(media_router)

BATCH_CONCURRENCY_LIMIT = MAX_CONCURRENT_CALLS

def _authorized(value: str | None) -> bool:
    if not ADMIN_USERNAME or not ADMIN_PASSWORD or not value or not value.startswith("Basic "): return False
    try: user,password=base64.b64decode(value[6:]).decode().split(":",1)
    except Exception: return False
    return hmac.compare_digest(user,ADMIN_USERNAME) and hmac.compare_digest(password,ADMIN_PASSWORD)

@app.middleware("http")
async def operator_auth(request: Request, call_next):
    protected=request.url.path=="/" or request.url.path.startswith("/api/") or request.url.path=="/twilio/outbound"
    if protected and not _authorized(request.headers.get("authorization")):
        return Response("Authentication required",401,{"WWW-Authenticate":"Basic"})
    return await call_next(request)


@app.get("/")
async def get_ui():
    return FileResponse(INDEX_HTML)



@app.get("/api/leads")
async def list_leads():
    return {"leads": await asyncio.to_thread(answer_store.list_leads)}


@app.post("/api/leads/preview")
async def preview_leads(file: UploadFile | None = File(default=None), pasted_data: str = Form(default="")):
    try:
        if file is not None:
            content = await file.read()
            headers, rows = answer_store.parse_upload(content, file.filename or "leads.csv")
        elif pasted_data.strip():
            headers, rows = answer_store.parse_upload(pasted_data.encode("utf-8"), "pasted.csv")
        else:
            raise HTTPException(status_code=400, detail="Upload a CSV/Excel file or paste tabular data.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("lead_preview_failed")
        raise HTTPException(status_code=400, detail=f"Unable to parse leads: {exc}") from exc
    if not headers and rows:
        headers = list(rows[0].keys())
    return {"headers": headers, "rows": rows, "preview_rows": rows[:25], "total_rows": len(rows)}


@app.post("/api/leads/import")
async def import_leads(payload: dict):
    rows = payload.get("rows", [])
    mapping = payload.get("mapping", {})
    normalized = []
    for row in rows:
        normalized.append({
            "business_name": row.get(mapping.get("business_name", ""), ""),
            "phone_number": row.get(mapping.get("phone_number", ""), ""),
            "category": row.get(mapping.get("category", ""), ""),
            "notes": row.get(mapping.get("notes", ""), ""),
        })
    result = await asyncio.to_thread(answer_store.import_leads, normalized)
    logger.info("lead_import_completed", extra={"submitted": len(rows), "imported": result["imported"], "rejected": len(result["rejected"])})
    return result


@app.post("/api/leads/manual")
async def manual_lead(payload: dict):
    row = {
        "business_name": payload.get("business_name", ""),
        "phone_number": payload.get("phone_number", ""),
        "category": payload.get("category", ""),
        "notes": payload.get("notes", ""),
    }
    result = await asyncio.to_thread(answer_store.import_leads, [row])
    if result["imported"] != 1:
        raise HTTPException(status_code=422, detail=result)
    return result


@app.post("/api/leads/{lead_id}/call")
async def call_lead(lead_id: str):
    lead = await asyncio.to_thread(answer_store.get_lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    payload = OutboundCallRequest(
        phone_number=lead["phone_number"],
        campaign_name="dashboard_lead",
        lead_id=lead_id,
        business_name=lead.get("business_name"),
        category=lead.get("category"),
        notes=lead.get("notes"),
    )
    try:
        logger.info("dashboard_lead_call_requested", extra={"lead_id": lead_id})
        result = await start_outbound_call(payload, idempotency_key=None)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("dashboard_lead_call_failed", extra={"lead_id": lead_id})
        await asyncio.to_thread(answer_store.update_lead, lead_id, status="call_failed", last_error=type(exc).__name__)
        raise HTTPException(status_code=502, detail="Unable to queue Twilio call") from exc


@app.delete("/api/leads/{lead_id}")
async def delete_lead(lead_id: str):
    deleted = await asyncio.to_thread(answer_store.delete_lead, lead_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Lead not found")
    logger.info("lead_deleted", extra={"lead_id": lead_id})
    return {"deleted": True, "lead_id": lead_id}


@app.post("/api/leads/call-batch")
async def call_lead_batch(payload: dict):
    requested_ids = set(payload.get("lead_ids") or [])
    max_calls = int(payload.get("max_calls") or 50)
    leads = await asyncio.to_thread(answer_store.list_leads)
    if requested_ids:
        leads = [lead for lead in leads if lead.get("lead_id") in requested_ids]
    leads = [lead for lead in leads if lead.get("phone_number") and lead.get("status") not in {"calling", "queued"}][:max_calls]
    if not leads:
        return {"requested": 0, "queued": 0, "concurrency_limit": BATCH_CONCURRENCY_LIMIT}
    queued=[]
    for lead in leads:
        try:
            call=await answer_store.aenqueue_call(phone_number=lead["phone_number"],lead_id=lead["lead_id"],business_name=lead.get("business_name", ""),category=lead.get("category", ""),notes=lead.get("notes", ""))
            queued.append(call["call_id"])
        except SuppressedError: continue
    return {"requested": len(leads), "queued": len(queued), "call_ids": queued, "concurrency_limit": BATCH_CONCURRENCY_LIMIT}


@app.get("/api/live-calls")
async def live_calls():
    return {"calls": await asyncio.to_thread(answer_store.list_live_calls, 100)}


@app.get("/api/calls")
async def list_calls(limit: int = 200, offset: int = 0):
    return {"calls": await asyncio.to_thread(answer_store.list_calls, limit, offset)}


@app.get("/api/calls/{call_id}")
async def get_call(call_id: str):
    call = await answer_store.aget_call(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


@app.delete("/api/calls/{call_id}")
async def delete_call(call_id: str):
    try:
        deleted = await asyncio.to_thread(answer_store.delete_call, call_id)
    except ActiveDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Call not found")
    logger.info("call_deleted", extra={"call_id": call_id})
    return {"deleted": True, "call_id": call_id}


@app.delete("/api/data")
async def clear_data(payload: dict):
    scope = payload.get("scope", "")
    if payload.get("confirmation") != f"DELETE {str(scope).upper()}":
        raise HTTPException(status_code=400, detail="Confirmation phrase does not match the requested deletion")
    try:
        deleted = await asyncio.to_thread(answer_store.clear_data, scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ActiveDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.warning("operator_data_cleared", extra={"scope": scope, **deleted})
    return {"deleted": deleted, "scope": scope}


@app.get("/api/stats")
async def stats():
    return await asyncio.to_thread(answer_store.statistics)


@app.get("/api/operations")
async def operations():
    return {
        "coordinator": coordinator.health(),
        "reconciliation": await asyncio.to_thread(answer_store.list_reconciliation, 100),
        # Any nonzero total here means Twilio callbacks are being rejected --
        # almost always a PUBLIC_BASE_URL that does not match the URL Twilio
        # signed. Nothing else in the system reports that condition.
        "twilio_signature_failures": signature_failure_health(),
        # What is currently occupying the queue. When calls sit in QUEUED and
        # nothing starts, this is the answer: something in capacity_occupied
        # is not finishing.
        "capacity": await asyncio.to_thread(answer_store.capacity_snapshot),
    }


@app.post("/api/calls/{call_id}/resolve")
async def resolve_call(call_id: str, payload: dict | None = None):
    """Force a stuck call terminal after checking the provider console.

    The queue deliberately refuses to guess a call's outcome from elapsed
    time. This is the escape hatch for when a human has established what
    actually happened and the provider will never tell us.
    """
    body = payload or {}
    status = str(body.get("status") or "failed").lower()
    try:
        call = await asyncio.to_thread(answer_store.resolve_stuck_call, call_id, status, str(body.get("note") or "operator_resolved"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    logger.warning("call_resolved_by_operator", extra={"call_id": call_id, "status": status})
    return {"resolved": True, "call_id": call_id, "provider_status": call.get("provider_status")}


@app.get("/api/leads/template")
async def lead_template():
    try:
        filename, content, media_type = answer_store.export_lead_template()
    except Exception as exc:
        logger.exception("lead_template_failed")
        raise HTTPException(status_code=500, detail=f"Unable to generate lead template: {exc}") from exc
    return Response(content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/export/{fmt}")
async def export_calls(fmt: str):
    if fmt not in {"xlsx", "csv", "json"}:
        raise HTTPException(status_code=400, detail="Format must be xlsx, csv, or json")
    filename, content, media_type = await asyncio.to_thread(answer_store.export_calls, fmt)
    return Response(content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "data_dir": str(DATA_DIR),
        "coordinator": coordinator.health(),
        "twilio_signature_failures": signature_failure_health(),
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if not _authorized(websocket.headers.get("authorization")):
        await websocket.close(code=1008); return
    adapter = BrowserAdapter(websocket)
    session = call_manager.create_session(campaign_name="browser", direction="browser")
    bridge = AudioBridge(session, call_result_service)
    adapter.audio_bridge = bridge
    adapter.attach(session)

    if not DEEPGRAM_API_KEY:
        await websocket.accept()
        await websocket.send_text(json.dumps({"error": "DEEPGRAM_API_KEY is not set on the server."}))
        await websocket.close()
        call_manager.destroy_session(session.call_id)
        return

    try:
        await adapter.start()
    finally:
        call_manager.destroy_session(session.call_id)
        print(f"Saved call {session.call_id} answers to {DATA_DIR}")


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=True)
