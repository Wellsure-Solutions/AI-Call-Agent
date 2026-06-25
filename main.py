import asyncio
import json
import threading

import uvicorn
from deepgram import DeepgramClient
from deepgram.core.events import EventType
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from answer_extractor import AnswerExtractor
from call_control import is_closing_call_message
from call_service import CallResultService
from config import DEEPGRAM_API_KEY, get_agent_settings
from excel_store import ExcelAnswerStore
from models import CallSession
from settings import ANSWERS_WORKBOOK, HOST, PORT

app = FastAPI(title="Autonomous Calling Agent")
answer_extractor = AnswerExtractor()
answer_store = ExcelAnswerStore(ANSWERS_WORKBOOK)
call_result_service = CallResultService(answer_extractor, answer_store)


@app.get("/")
async def get_ui():
    return FileResponse("static/index.html")


@app.get("/health")
async def health_check():
    return {"status": "ok", "answers_workbook": str(ANSWERS_WORKBOOK)}


class DeepgramCallBridge:
    """Bridges browser/Asterisk audio to Deepgram and persists temporary call answers."""

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.session = CallSession()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.message_queue: asyncio.Queue[tuple[str, bytes | str]] = asyncio.Queue()
        self.client_task: asyncio.Task | None = None
        self.closing_requested = False

    async def run(self) -> None:
        await self.websocket.accept()
        if not DEEPGRAM_API_KEY:
            await self.websocket.send_text(json.dumps({"error": "DEEPGRAM_API_KEY is not set on the server."}))
            await self.websocket.close()
            return

        self.loop = asyncio.get_running_loop()
        client = DeepgramClient(api_key=DEEPGRAM_API_KEY)
        close_status = "completed"

        try:
            with client.agent.v1.connect() as connection:
                self._register_handlers(connection)
                connection.send_settings(get_agent_settings())

                threading.Thread(target=connection.start_listening, daemon=True).start()
                self.client_task = asyncio.create_task(self._send_to_client())

                while not self.closing_requested:
                    client_audio_data = await self.websocket.receive_bytes()
                    if client_audio_data:
                        connection.send_media(client_audio_data)
        except WebSocketDisconnect:
            close_status = "client_disconnected"
            print(f"Client disconnected for call {self.session.call_id}.")
        except Exception as exc:
            close_status = "error"
            print(f"Connection error for call {self.session.call_id}: {exc}")
        finally:
            if self.client_task is not None:
                self.client_task.cancel()
            self._persist_call(close_status)

    def _register_handlers(self, connection) -> None:
        connection.on(EventType.OPEN, lambda _event: print(f"[deepgram] opened call {self.session.call_id}"))
        connection.on(EventType.MESSAGE, self._on_message)
        connection.on(EventType.CLOSE, lambda _event: print(f"[deepgram] closed call {self.session.call_id}"))
        connection.on(EventType.ERROR, self._on_error)

    def _on_message(self, message) -> None:
        try:
            if isinstance(message, bytes):
                self._queue_threadsafe("audio", message)
                return

            role = getattr(message, "role", None)
            content = getattr(message, "content", None)
            msg_type = getattr(message, "type", "Unknown")
            print(f"[deepgram] call={self.session.call_id} message type={msg_type}")

            if is_closing_call_message(message, content):
                self.closing_requested = True
                self._queue_threadsafe("control", json.dumps({"event": "closing_call"}))
                return

            if msg_type == "ConversationText" or (role and content):
                self.session.add_turn(role, content)
                payload = json.dumps({"role": role or "agent", "content": content or ""})
                self._queue_threadsafe("text", payload)
        except Exception as exc:
            print(f"[deepgram] handler error for call {self.session.call_id}: {exc}")

    def _on_error(self, error) -> None:
        print(f"[deepgram] call={self.session.call_id} error: {error}")
        self._queue_threadsafe("text", json.dumps({"error": str(error)}))

    def _queue_threadsafe(self, message_type: str, data: bytes | str) -> None:
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(self.message_queue.put((message_type, data)), self.loop)

    async def _send_to_client(self) -> None:
        try:
            while True:
                message_type, data = await self.message_queue.get()
                if message_type == "audio" and isinstance(data, bytes):
                    await self.websocket.send_bytes(data)
                elif message_type == "text" and isinstance(data, str):
                    await self.websocket.send_text(data)
                elif message_type == "control" and isinstance(data, str):
                    await self.websocket.send_text(data)
                    await self.websocket.close(code=1000, reason="agent_closing_call")
                    self.closing_requested = True
                    break
        except asyncio.CancelledError:
            pass

    def _persist_call(self, status: str) -> None:
        call_result_service.finalize(self.session, status)
        print(f"Saved call {self.session.call_id} answers to {ANSWERS_WORKBOOK}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await DeepgramCallBridge(websocket).run()


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
