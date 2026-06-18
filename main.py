import json
import threading
import asyncio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from deepgram import DeepgramClient
from deepgram.core.events import EventType

# Import settings from our new config file
from config import DEEPGRAM_API_KEY, get_agent_settings

app = FastAPI()

@app.get("/")
async def get_ui():
    # Serves the frontend file from the static folder
    return FileResponse("static/index.html")
    

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    if not DEEPGRAM_API_KEY:
        await websocket.send_text(json.dumps({"error": "DEEPGRAM_API_KEY is not set on the server."}))
        await websocket.close()
        return

    client = DeepgramClient(api_key=DEEPGRAM_API_KEY)

    loop = asyncio.get_running_loop()
    message_queue: asyncio.Queue = asyncio.Queue()

    # ---- Event handlers ----

    def on_open_handler(_open_event):
        print("[deepgram] connection opened")

    def on_message_handler(message):
        try:
            if isinstance(message, bytes):
                asyncio.run_coroutine_threadsafe(message_queue.put(("audio", message)), loop)
                return

            msg_type = getattr(message, "type", "Unknown")
            print(f"[deepgram] message type={msg_type}")

            role = getattr(message, "role", None)
            content = getattr(message, "content", None)
            if msg_type == "ConversationText" or (role and content):
                payload = json.dumps({"role": role or "agent", "content": content or ""})
                asyncio.run_coroutine_threadsafe(message_queue.put(("text", payload)), loop)
        except Exception as e:
            print(f"[deepgram] error in on_message_handler: {e}")

    def on_close_handler(_close_event):
        print("[deepgram] connection closed")

    def on_error_handler(error):
        print(f"[deepgram] error: {error}")
        try:
            payload = json.dumps({"error": str(error)})
            asyncio.run_coroutine_threadsafe(message_queue.put(("text", payload)), loop)
        except Exception:
            pass

    async def send_to_client():
        try:
            while True:
                msg_type, data = await message_queue.get()
                if msg_type == "audio":
                    await websocket.send_bytes(data)
                elif msg_type == "text":
                    await websocket.send_text(data)
        except asyncio.CancelledError:
            pass

    listen_thread = None
    client_task = None

    try:
        with client.agent.v1.connect() as connection:
            connection.on(EventType.OPEN, on_open_handler)
            connection.on(EventType.MESSAGE, on_message_handler)
            connection.on(EventType.CLOSE, on_close_handler)
            connection.on(EventType.ERROR, on_error_handler)

            # Retrieve settings defined in config.py
            settings = get_agent_settings()
            connection.send_settings(settings)

            listen_thread = threading.Thread(target=connection.start_listening, daemon=True)
            listen_thread.start()

            client_task = asyncio.create_task(send_to_client())

            while True:
                client_audio_data = await websocket.receive_bytes()
                if client_audio_data:
                    connection.send_media(client_audio_data)

    except WebSocketDisconnect:
        print("Client disconnected.")
    except Exception as e:
        print(f"Connection error: {e}")
    finally:
        if client_task is not None:
            client_task.cancel()

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)