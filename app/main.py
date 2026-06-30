import json

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse

from app.integrations.deepgram.config import DEEPGRAM_API_KEY
from app.services.answer_extractor import AnswerExtractor
from app.services.call_service import CallResultService
from app.storage.excel_store import ExcelAnswerStore
from app.core.settings import ANSWERS_WORKBOOK, HOST, INDEX_HTML, PORT
from app.telephony.adapters.browser_adapter import BrowserAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.call_manager import CallManager

app = FastAPI(title="Autonomous Calling Agent")
answer_extractor = AnswerExtractor()
answer_store = ExcelAnswerStore(ANSWERS_WORKBOOK)
call_result_service = CallResultService(answer_extractor, answer_store)
call_manager = CallManager()


@app.get("/")
async def get_ui():
    return FileResponse(INDEX_HTML)


@app.get("/health")
async def health_check():
    return {"status": "ok", "answers_workbook": str(ANSWERS_WORKBOOK)}


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
        print(f"Saved call {session.call_id} answers to {ANSWERS_WORKBOOK}")


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=True)
