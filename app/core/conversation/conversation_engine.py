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
InterruptedCallback = Callable[[], None]

# Deepgram's Agent API closes an idle WebSocket ~10 seconds after it last
# received audio or a KeepAlive message (see
# https://developers.deepgram.com/docs/agent-keep-alive). This engine's
# connection is opened *before* the phone is answered (pre-warmed for a fast
# greeting), so it can sit idle for the entire ring duration -- which is
# almost always longer than 10 seconds -- unless kept alive explicitly.
DEEPGRAM_KEEPALIVE_INTERVAL_SECONDS = 5


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
    """Deepgram-backed conversation engine with adapter-specific audio."""

    def __init__(
        self,
        session: CallSession,
        on_audio: AudioCallback,
        on_text: TextCallback,
        on_finished: FinishedCallback,
        on_interrupted: InterruptedCallback | None = None,
    ) -> None:
        self.session = session
        self.on_audio = on_audio
        self.on_text = on_text
        self.on_finished = on_finished
        self.on_interrupted = on_interrupted
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connection = None
        self._connection_context = None
        self.closing_requested = False
        self._close_after_audio_done = False
        self._send_lock = asyncio.Lock()
        self._deepgram_closed = False
        self._keepalive_task: asyncio.Task | None = None

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
        self.connection.send_settings(
            get_agent_settings(
                self.session.metadata,
                transport=self.session.direction,
            )
        )
        self.session.safe_transition_to(CallState.AI_ACTIVE)
        threading.Thread(target=self.connection.start_listening, daemon=True).start()
        # The call may sit ringing for well over Deepgram's ~10s idle
        # timeout before Twilio answers and real audio starts flowing.
        # Without this, the pre-warmed connection (and the greeting audio
        # already generated on it) is dead by the time anyone picks up.
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def receive_audio(self, audio_frame: bytes) -> bool:
        if self.connection is None or self.closing_requested or self._deepgram_closed or not audio_frame:
            return False
        try:
            # The Deepgram SDK wraps a synchronous websocket. Serialize writes
            # per call and move the blocking send off the event loop so batch
            # calls cannot stall unrelated Twilio handshakes.
            async with self._send_lock:
                if self.connection is None or self.closing_requested or self._deepgram_closed:
                    return False
                await asyncio.to_thread(self.connection.send_media, audio_frame)
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

    async def _keepalive_loop(self) -> None:
        """Sends periodic KeepAlive control messages so Deepgram's idle
        timeout never fires while we're waiting for the call to be answered
        (or, generally, whenever real audio momentarily stops flowing).
        Safe to run concurrently with real audio -- sending both is fine per
        Deepgram's docs; this simply guarantees the gap between audio frames
        never exceeds the 10s window.
        """
        try:
            while True:
                await asyncio.sleep(DEEPGRAM_KEEPALIVE_INTERVAL_SECONDS)
                if self.connection is None or self._deepgram_closed or self.closing_requested:
                    return
                try:
                    async with self._send_lock:
                        if self.connection is None or self._deepgram_closed or self.closing_requested:
                            return
                        await asyncio.to_thread(self.connection.send_keep_alive)
                except Exception as exc:
                    logger.warning(
                        "deepgram_keepalive_send_failed",
                        extra={"call_id": self.session.call_id, "error": str(exc)},
                    )
                    return
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        self.closing_requested = True
        self._deepgram_closed = True
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._keepalive_task
            self._keepalive_task = None
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

            if msg_type == "UserStartedSpeaking":
                if self.on_interrupted is not None:
                    self._call_threadsafe(self.on_interrupted)
                return

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