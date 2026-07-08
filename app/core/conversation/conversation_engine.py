from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import asdict, is_dataclass
from typing import Callable

from deepgram import DeepgramClient
from deepgram.clients.agent.enums import AgentWebSocketEvents

from app.integrations.deepgram.config import DEEPGRAM_API_KEY, get_agent_settings
from app.services.call_control import is_closing_call_message, is_terminal_assistant_text
from app.services.transcript_sanitizer import strip_spoken_internal_commands
from app.telephony.call_session import CallSession
from app.telephony.state_machine import CallState

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

    async def start(self) -> None:
        if not DEEPGRAM_API_KEY:
            raise RuntimeError("DEEPGRAM_API_KEY is not set on the server.")
        self.loop = asyncio.get_running_loop()
        client = DeepgramClient(api_key=DEEPGRAM_API_KEY)
        self._connection_context = client.agent.v1.connect()
        self.connection = self._connection_context.__enter__()
        self.session.deepgram_connection = self.connection
        self._register_handlers(self.connection)
        self.connection.send_settings(get_agent_settings(self.session.metadata.get("lead")))
        self.session.safe_transition_to(CallState.AI_ACTIVE)
        threading.Thread(target=self.connection.start_listening, daemon=True).start()

    async def receive_audio(self, pcm_frame: bytes) -> None:
        if self.connection is not None and pcm_frame:
            self.connection.send_media(pcm_frame)

    async def stop(self) -> None:
        if self._connection_context is not None:
            self._connection_context.__exit__(None, None, None)
            self._connection_context = None
            self.connection = None

    def _register_handlers(self, connection) -> None:
        connection.on(AgentWebSocketEvents.Open, lambda _event: print(f"[deepgram] opened call {self.session.call_id}"))
        connection.on(AgentWebSocketEvents.ConversationText, self._on_message)
        connection.on(AgentWebSocketEvents.AudioData, self._on_message)
        connection.on(AgentWebSocketEvents.AgentAudioDone, self._on_message)
        connection.on(AgentWebSocketEvents.Close, lambda _event: print(f"[deepgram] closed call {self.session.call_id}"))
        connection.on(AgentWebSocketEvents.Error, self._on_error)

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

    def _on_error(self, error) -> None:
        error_payload = safe_event_payload(error)
        print(f"[deepgram] call={self.session.call_id} error: {error_payload}")
        self._call_threadsafe(lambda: self.on_text(json.dumps({"error": "Deepgram agent error", "details": error_payload})))

    def _call_threadsafe(self, callback: Callable[[], None]) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(callback)
