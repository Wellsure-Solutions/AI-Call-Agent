import json
from collections import Counter

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket
from fastapi.responses import FileResponse, Response

from app.integrations.deepgram.config import DEEPGRAM_API_KEY
from app.services.answer_extractor import AnswerExtractor
from app.services.call_service import CallResultService
from app.storage.json_store import JsonCallStore
from app.core.settings import DATA_DIR, HOST, INDEX_HTML, PORT
from app.telephony.adapters.browser_adapter import BrowserAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.call_manager import CallManager
from app.telephony.twilio_routes import media_router, router as twilio_router

app = FastAPI(title="Autonomous Calling Agent")
answer_extractor = AnswerExtractor()
answer_store = JsonCallStore(DATA_DIR)
call_result_service = CallResultService(answer_extractor, answer_store)
call_manager = CallManager()
app.include_router(twilio_router)
app.include_router(media_router)

@app.get("/")
async def get_ui():
    return FileResponse(INDEX_HTML)



@app.get("/api/leads")
async def list_leads():
    return {"leads": answer_store.list_leads()}


@app.post("/api/leads/preview")
async def preview_leads(file: UploadFile | None = File(default=None), pasted_data: str = Form(default="")):
    if file is not None:
        content = await file.read()
        headers, rows = answer_store.parse_upload(content, file.filename or "leads.csv")
    elif pasted_data.strip():
        headers, rows = answer_store.parse_upload(pasted_data.encode("utf-8"), "pasted.csv")
    else:
        raise HTTPException(status_code=400, detail="Upload a CSV/Excel file or paste tabular data.")
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
    return answer_store.import_leads(normalized)


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
