import asyncio
import json
import logging
import time
from collections import Counter

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile, WebSocket
from fastapi.responses import FileResponse, Response

from app.integrations.deepgram.config import DEEPGRAM_API_KEY
from app.services.answer_extractor import AnswerExtractor
from app.services.call_service import CallResultService
from app.services.call_status_tracker import call_status_tracker
from app.storage.json_store import JsonCallStore
from app.core.settings import DATA_DIR, HOST, INDEX_HTML, PORT
from app.telephony.adapters.browser_adapter import BrowserAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.call_manager import CallManager
from app.telephony.twilio_routes import OutboundCallRequest, media_router, router as twilio_router, start_outbound_call

logger = logging.getLogger(__name__)

app = FastAPI(title="Autonomous Calling Agent")
answer_extractor = AnswerExtractor()
answer_store = JsonCallStore(DATA_DIR)
call_result_service = CallResultService(answer_extractor, answer_store)
call_manager = CallManager()
app.include_router(twilio_router)
app.include_router(media_router)

BATCH_CONCURRENCY_LIMIT = 1
BATCH_START_DELAY_SECONDS = 2
BATCH_TERMINAL_STATUSES = {"completed", "failed", "busy", "no-answer", "canceled", "finished", "result_save_failed", "call_failed"}

# Safety net: if a lead's status callback (from /twilio/status or
# media_stream's own cleanup) never arrives -- dropped webhook, Twilio
# outage, unhandled exception somewhere in the pipeline -- this guarantees
# the batch runner still moves on instead of stalling forever on a single
# lead. Generous enough to cover full ring time + a real conversation.
BATCH_MAX_WAIT_SECONDS = 90


async def _run_batch_calls(leads: list[dict]) -> None:
    """Run dashboard batch calls with a conservative live-call limit.

    The runner starts a replacement only after one of the active leads leaves
    the calling state, so large uploads are drained without exposing lead data
    in the API response.

    A lead is considered "done" (and replaced by the next queued lead) when
    ANY of the following is true:
      - its stored status has moved away from "calling"
      - call_status_tracker reports a terminal status for its call
      - BATCH_MAX_WAIT_SECONDS has elapsed since the call was placed
        (defensive timeout -- see note above)
    """
    queue = list(leads)
    active: dict[str, dict] = {}

    async def start_next() -> None:
        while queue and len(active) < BATCH_CONCURRENCY_LIMIT:
            lead = queue.pop(0)
            lead_id = lead.get("lead_id")
            outbound = OutboundCallRequest(
                phone_number=lead["phone_number"],
                campaign_name="dashboard_batch",
                lead_id=lead_id,
                business_name=lead.get("business_name"),
                category=lead.get("category"),
                notes=lead.get("notes"),
            )
            try:
                logger.info("dashboard_batch_call_requested", extra={"lead_id": lead_id})
                result = await start_outbound_call(outbound)
                answer_store.update_lead(lead_id, status="calling", last_call_id=result.get("call_id"), last_call_sid=result.get("call_sid"))
                active[lead_id] = {"call_id": result.get("call_id"), "started_at": time.monotonic()}
                await asyncio.sleep(BATCH_START_DELAY_SECONDS)
            except Exception as exc:
                logger.exception("dashboard_batch_call_failed", extra={"lead_id": lead_id})
                answer_store.update_lead(lead_id, status="call_failed", last_error=str(exc))

    await start_next()
    while active or queue:
        await asyncio.sleep(5)
        completed: list[str] = []
        for lead_id, metadata in active.items():
            lead = answer_store.get_lead(lead_id)
            status = (lead or {}).get("status")
            call_id = metadata.get("call_id")
            live_status = (call_status_tracker.get(call_id) or {}).get("status") if call_id else None
            elapsed = time.monotonic() - metadata.get("started_at", time.monotonic())
            timed_out = elapsed > BATCH_MAX_WAIT_SECONDS

            if (status and status != "calling") or live_status in BATCH_TERMINAL_STATUSES or timed_out:
                if timed_out and not (status and status != "calling") and live_status not in BATCH_TERMINAL_STATUSES:
                    logger.warning(
                        "dashboard_batch_call_timed_out",
                        extra={"lead_id": lead_id, "call_id": call_id, "elapsed_seconds": round(elapsed, 1)},
                    )
                    answer_store.update_lead(lead_id, status="call_failed", last_error="timed out waiting for call status to resolve")
                completed.append(lead_id)
        for lead_id in completed:
            active.pop(lead_id, None)
        await start_next()


@app.get("/")
async def get_ui():
    return FileResponse(INDEX_HTML)



@app.get("/api/leads")
async def list_leads():
    return {"leads": answer_store.list_leads()}


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
    result = answer_store.import_leads(normalized)
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
    result = answer_store.import_leads([row])
    if result["imported"] != 1:
        raise HTTPException(status_code=422, detail=result)
    return result


@app.post("/api/leads/{lead_id}/call")
async def call_lead(lead_id: str):
    lead = answer_store.get_lead(lead_id)
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
        result = await start_outbound_call(payload)
        answer_store.update_lead(lead_id, status="calling", last_call_id=result.get("call_id"), last_call_sid=result.get("call_sid"))
        return result
    except Exception as exc:
        logger.exception("dashboard_lead_call_failed", extra={"lead_id": lead_id, "phone_number": lead.get("phone_number")})
        answer_store.update_lead(lead_id, status="call_failed", last_error=str(exc))
        raise HTTPException(status_code=502, detail=f"Unable to start Twilio call: {exc}") from exc


@app.post("/api/leads/call-batch")
async def call_lead_batch(payload: dict, background_tasks: BackgroundTasks):
    requested_ids = set(payload.get("lead_ids") or [])
    max_calls = int(payload.get("max_calls") or 50)
    leads = answer_store.list_leads()
    if requested_ids:
        leads = [lead for lead in leads if lead.get("lead_id") in requested_ids]
    leads = [lead for lead in leads if lead.get("phone_number") and lead.get("status") not in {"calling", "queued"}][:max_calls]
    if not leads:
        return {"requested": 0, "queued": 0, "concurrency_limit": BATCH_CONCURRENCY_LIMIT}
    for lead in leads:
        answer_store.update_lead(lead.get("lead_id"), status="queued")
    background_tasks.add_task(_run_batch_calls, leads)
    return {"requested": len(leads), "queued": len(leads), "concurrency_limit": BATCH_CONCURRENCY_LIMIT}


@app.get("/api/live-calls")
async def live_calls():
    return {"calls": call_status_tracker.list()}


@app.get("/api/calls")
async def list_calls():
    return {"calls": answer_store.list_calls()}


@app.get("/api/calls/{call_id}")
async def get_call(call_id: str):
    call = answer_store.get_call(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


@app.get("/api/stats")
async def stats():
    calls = answer_store.list_calls()
    leads = answer_store.list_leads()
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