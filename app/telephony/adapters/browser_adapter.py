from __future__ import annotations

import asyncio

from fastapi import WebSocket, WebSocketDisconnect

from app.telephony.adapters.base import BaseTelephonyAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.call_session import CallSession


class BrowserAdapter(BaseTelephonyAdapter):
    """Browser WebSocket adapter preserving /ws behavior with safe final-audio drain."""

    def __init__(self, websocket: WebSocket, audio_bridge: AudioBridge | None = None) -> None:
        super().__init__()
        self.websocket = websocket
        self.audio_bridge = audio_bridge
        self.client_task: asyncio.Task | None = None
        self.closing_requested = False

    def attach(self, session: CallSession) -> None:
        super().attach(session)
        if self.audio_bridge is None:
            self.audio_bridge = AudioBridge(session)

    async def connect(self) -> None:
        await self.websocket.accept()

    async def disconnect(self) -> None:
        await self.websocket.close()

    async def send_audio(self, audio_frame: bytes) -> None:
        await self.websocket.send_bytes(audio_frame)

    async def receive_audio(self) -> bytes | None:
        return await self.websocket.receive_bytes()

    async def hangup(self) -> None:
        self.closing_requested = True
        await asyncio.sleep(0.35)
        await self.websocket.close(code=1000, reason="agent_closing_call")

    async def answer(self) -> None:
        await self.connect()

    async def start(self) -> None:
        if self.session is None or self.audio_bridge is None:
            raise RuntimeError("BrowserAdapter must be attached to a CallSession before start().")

        await self.answer()
        await self.audio_bridge.start()
        self.client_task = asyncio.create_task(self._send_to_browser())

        close_status = "completed"
        try:
            while not self.closing_requested:
                audio = await self.receive_audio()
                if audio:
                    accepted = await self.audio_bridge.receive_telephony_audio(audio)
                    if not accepted:
                        close_status = "completed"
                        break
        except WebSocketDisconnect:
            # If the agent already requested a normal close, do not label it as
            # a client failure merely because our own socket close wakes receive().
            close_status = "completed" if self.closing_requested else "client_disconnected"
            if close_status == "client_disconnected":
                print(f"Client disconnected for call {self.session.call_id}.")
        except Exception as exc:
            close_status = "error"
            print(f"Connection error for call {self.session.call_id}: {exc}")
        finally:
            if self.client_task is not None:
                self.client_task.cancel()
                try:
                    await self.client_task
                except asyncio.CancelledError:
                    pass
            await self.audio_bridge.stop(close_status)

    async def stop(self) -> None:
        self.closing_requested = True
        if self.audio_bridge is not None:
            await self.audio_bridge.stop()

    async def _send_to_browser(self) -> None:
        assert self.audio_bridge is not None
        try:
            while True:
                message_type, data = await self.audio_bridge.next_output()

                if message_type == "audio" and isinstance(data, bytes):
                    await self.send_audio(data)

                elif message_type == "text" and isinstance(data, str):
                    await self.websocket.send_text(data)

                elif message_type == "interrupt" and isinstance(data, str):
                    await self.websocket.send_text(data)

                elif message_type == "control" and isinstance(data, str):
                    # Send the final control event, but do not tear the WebSocket
                    # down on the exact same event-loop tick. Deepgram's
                    # AgentAudioDone means synthesis is complete, while the
                    # browser can still have a small playback buffer.
                    await self.websocket.send_text(data)
                    self.closing_requested = True
                    await asyncio.sleep(0.35)
                    try:
                        await self.websocket.close(
                            code=1000,
                            reason="agent_closing_call",
                        )
                    except RuntimeError:
                        pass
                    break

        except asyncio.CancelledError:
            pass
        except (WebSocketDisconnect, RuntimeError):
            pass
