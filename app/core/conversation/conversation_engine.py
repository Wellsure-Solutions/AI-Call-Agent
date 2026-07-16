from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import suppress
from dataclasses import asdict, is_dataclass
from typing import Callable

from deepgram import DeepgramClient
from deepgram.core.events import EventType

from app.integrations.deepgram.config import DEEPGRAM_API_KEY, get_agent_settings
from app.services.call_control import is_closing_call_message, is_terminal_assistant_text
from app.services.transcript_sanitizer import strip_spoken_internal_commands
from app.telephony.call_session import CallSession
from app.telephony.state_machine import CallState

logger = logging.getLogger(__name__)

AudioCallback = Callable[[bytes], None]
TextCallback = Callable[[str], None]
FinishedCallback = Callable[[], None]


def safe_event_payload(event) -> dict[str, object]:
    """Return a serializable event payload for Deepgram diagnostics."""
    if event is None:
        return {}
    if is_dataclass(event):
        return asdict(event)
    if isinstance(event, dict):
        return event
    payload: dict[str, object] = {}
    for name in ("type", "description", "message", "code", "variant", "role", "content"):
        value = getattr(event, name, None)
        if value is not None:
            payload[name] = value
    if not payload:
        payload["repr"] = repr(event)
    return payload


class ConversationEngine:
    """Deepgram-backed PCM conversation engine with no telephony dependencies."""

    def __init__(
        self,
        session: CallSession,
        on_audio: AudioCallback,
        on_text: TextCallback,
        on_finished: FinishedCallback,
    ) -> None:
        self.session = session
        self.on_audio = on_audio
        self.on_text = on_text
        self.on_finished = on_finished
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connection = None
        self._connection_context = None
        self.closing_requested = False
        self._close_after_audio_done = False
        self._send_lock = asyncio.Lock()
        self._deepgram_closed = False

    async def start(self) -> None:
        if not DEEPGRAM_API_KEY:
            raise RuntimeError("DEEPGRAM_API_KEY is not set on the server.")
        self.loop = asyncio.get_running_loop()
        client = DeepgramClient(api_key=DEEPGRAM_API_KEY)
        self._connection_context = client.agent.v1.connect()
        self.connection = self._connection_context.__enter__()
        self._deepgram_closed = False
        self.session.deepgram_connection = self.connection
        self._register_handlers(self.connection)
        self.connection.send_settings(get_agent_settings(self.session.metadata))
        self.session.safe_transition_to(CallState.AI_ACTIVE)
        threading.Thread(target=self.connection.start_listening, daemon=True).start()

    async def receive_audio(self, pcm_frame: bytes) -> bool:
        if self.connection is None or self.closing_requested or self._deepgram_closed or not pcm_frame:
            return False
        try:
            # The Deepgram SDK wraps a synchronous websocket. Serialize writes
            # per call and move the blocking send off the event loop so batch
            # calls cannot stall unrelated Twilio handshakes.
            async with self._send_lock:
                if self.connection is None or self.closing_requested or self._deepgram_closed:
                    return False
                await asyncio.to_thread(self.connection.send_media, pcm_frame)
            return True
        except Exception as exc:
            self.closing_requested = True
            self._deepgram_closed = True
            self.session.metadata["deepgram_send_error"] = str(exc)
            if self._is_normal_deepgram_close(exc):
                logger.info(
                    "deepgram_send_media_closed",
                    extra={"call_id": self.session.call_id, "details": str(exc)},
                )
            else:
                logger.exception("deepgram_send_media_failed", extra={"call_id": self.session.call_id})
            self._call_threadsafe(self.on_finished)
            return False

    async def stop(self) -> None:
        self.closing_requested = True
        self._deepgram_closed = True
        if self._connection_context is not None:
            with suppress(Exception):
                self._connection_context.__exit__(None, None, None)
            self._connection_context = None
            self.connection = None

    def _register_handlers(self, connection) -> None:
        connection.on(EventType.OPEN, lambda _event: print(f"[deepgram] opened call {self.session.call_id}"))
        connection.on(EventType.MESSAGE, self._on_message)
        connection.on(EventType.CLOSE, self._on_close)
        connection.on(EventType.ERROR, self._on_error)

    def _on_message(self, message) -> None:
        try:
            if isinstance(message, bytes):
                self._call_threadsafe(lambda: self.on_audio(message))
                return

            role = getattr(message, "role", None)
            content = getattr(message, "content", None)
            msg_type = getattr(message, "type", "Unknown")
            print(f"[deepgram] call={self.session.call_id} message type={msg_type} payload={safe_event_payload(message)}")

            if is_closing_call_message(message, content):
                self.closing_requested = True
                self.session.safe_transition_to(CallState.AI_FINISHED)
                self._call_threadsafe(self.on_finished)
                return

            if msg_type == "AgentAudioDone" and self._close_after_audio_done:
                self.closing_requested = True
                self.session.safe_transition_to(CallState.AI_FINISHED)
                self._call_threadsafe(self.on_finished)
                return

            if msg_type == "ConversationText" or (role and content):
                cleaned_content = strip_spoken_internal_commands(content) if role == "assistant" else (content or "")
                if not cleaned_content:
                    return
                self.session.add_turn(role, cleaned_content)
                if role == "assistant" and is_terminal_assistant_text(cleaned_content):
                    self._close_after_audio_done = True
                payload = json.dumps({"role": role or "agent", "content": cleaned_content})
                self._call_threadsafe(lambda: self.on_text(payload))
        except Exception as exc:
            print(f"[deepgram] handler error for call {self.session.call_id}: {exc}")

    def _on_close(self, event) -> None:
        self._deepgram_closed = True
        print(f"[deepgram] closed call {self.session.call_id} payload={safe_event_payload(event)}")

    def _is_normal_deepgram_close(self, exc: Exception) -> bool:
        text = f"{type(exc).__name__}: {exc}"
        return "ConnectionClosedOK" in text or "received 1000" in text or "received 1005" in text

    def _on_error(self, error) -> None:
        error_payload = safe_event_payload(error)
        self.closing_requested = True
        self._deepgram_closed = True
        self.session.metadata["deepgram_error"] = error_payload
        logger.error("deepgram_agent_error", extra={"call_id": self.session.call_id, "details": error_payload})
        print(f"[deepgram] call={self.session.call_id} error: {error_payload}")
        self._call_threadsafe(lambda: self.on_text(json.dumps({"error": "Deepgram agent error", "details": error_payload})))
        self._call_threadsafe(self.on_finished)

    def _call_threadsafe(self, callback: Callable[[], None]) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(callback)
