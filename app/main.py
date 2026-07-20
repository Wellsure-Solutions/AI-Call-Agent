import asyncio
import json
import logging
import time
import base64
import hmac
from contextlib import asynccontextmanager
from collections import Counter

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, Response

from app.integrations.deepgram.config import DEEPGRAM_API_KEY
from app.services.answer_extractor import AnswerExtractor
from app.services.call_service import CallResultService
from app.storage.sqlite_store import SQLiteCallStore, SuppressedError
from app.core.settings import ADMIN_PASSWORD, ADMIN_USERNAME, DATA_DIR, DATABASE_PATH, HOST, INDEX_HTML, MAX_CONCURRENT_CALLS, PORT, START_INTERVAL_SECONDS, EXTRACTION_MAX_ATTEMPTS, EXTRACTION_TIMEOUT_SECONDS
from app.services.call_coordinator import DurableCallCoordinator
from app.telephony.adapters.browser_adapter import BrowserAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.call_manager import CallManager
from app.telephony.twilio_routes import OutboundCallRequest, configure as configure_twilio, media_router, router as twilio_router, start_outbound_call

logger = logging.getLogger(__name__)

answer_extractor = AnswerExtractor()
answer_store = SQLiteCallStore(DATABASE_PATH, DATA_DIR)
call_result_service = CallResultService(answer_extractor, answer_store, timeout=EXTRACTION_TIMEOUT_SECONDS, max_attempts=EXTRACTION_MAX_ATTEMPTS)
call_manager = CallManager()
configure_twilio(answer_store, call_result_service)
coordinator = DurableCallCoordinator(answer_store, MAX_CONCURRENT_CALLS, START_INTERVAL_SECONDS)

@asynccontextmanager
async def lifespan(_: FastAPI):
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
        logger.info("dashboard_lead_call_requested", extra={"lead_id": lead_id, "phone_number": lead.get("phone_number")})
        result = await start_outbound_call(payload, idempotency_key=None)
        return result
    except Exception as exc:
        logger.exception("dashboard_lead_call_failed", extra={"lead_id": lead_id, "phone_number": lead.get("phone_number")})
        await asyncio.to_thread(answer_store.update_lead, lead_id, status="call_failed", last_error=type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"Unable to start Twilio call: {exc}") from exc


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
async def list_calls():
    return {"calls": await asyncio.to_thread(answer_store.list_calls, 200, 0)}


@app.get("/api/calls/{call_id}")
async def get_call(call_id: str):
    call = await answer_store.aget_call(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


@app.get("/api/stats")
async def stats():
    calls = await asyncio.to_thread(answer_store.list_calls, 500, 0)
    leads = await asyncio.to_thread(answer_store.list_leads)
    total = len(calls)
    answered = sum(1 for c in calls if c.get("duration", 0) > 0 and c.get("call_status") not in {"failed", "busy", "no-answer"})
    busy_failed = sum(1 for c in calls if c.get("call_status") in {"failed", "busy"})
    interested = sum(1 for c in calls if c.get("interested"))
    callbacks = sum(1 for c in calls if c.get("callback_requested"))
    outcomes = Counter(c.get("call_status", "unknown") for c in calls)
    by_day = Counter(str(c.get("timestamp", ""))[:10] for c in calls if c.get("timestamp"))
    avg_duration = round(sum(int(c.get("duration") or 0) for c in calls) / total, 1) if total else 0
    return {
        "total_leads": len(leads),
        "total_calls": total,
        "calls_answered": answered,
        "calls_not_answered": max(total - answered - busy_failed, 0),
        "busy_failed_calls": busy_failed,
        "average_call_duration": avg_duration,
        "interested_responses": interested,
        "callback_requested": callbacks,
        "success_rate": round((interested / total) * 100, 1) if total else 0,
        "outcomes": dict(outcomes),
        "calls_over_time": dict(sorted(by_day.items())),
    }


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
    filename, content, media_type = answer_store.export_calls(fmt)
    return Response(content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.get("/health")
async def health_check():
    return {"status": "ok", "data_dir": str(DATA_DIR)}


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
