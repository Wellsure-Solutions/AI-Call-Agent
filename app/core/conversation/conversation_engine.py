from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import suppress
from dataclasses import asdict, is_dataclass
from typing import Callable

from deepgram import DeepgramClient
from deepgram.agent.v1.types import AgentV1SendFunctionCallResponse
from deepgram.core.events import EventType

from app.core.settings import AGENT_CLOSE_GRACE_SECONDS
from app.integrations.deepgram.config import DEEPGRAM_API_KEY, get_agent_settings
from app.services.call_control import (
    END_CALL_FUNCTION,
    is_closing_call_message,
    is_terminal_assistant_text,
    normalize_end_call_reason,
)
from app.services.transcript_sanitizer import strip_spoken_internal_commands
from app.telephony.call_session import CallSession
from app.telephony.metrics import CallMetrics
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
        metrics: CallMetrics | None = None,
    ) -> None:
        self.session = session
        self.metrics = metrics
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
        self._close_deadline_task = None

    async def start(self) -> None:
        if not DEEPGRAM_API_KEY:
            raise RuntimeError("DEEPGRAM_API_KEY is not set on the server.")
        self.loop = asyncio.get_running_loop()
        client = DeepgramClient(api_key=DEEPGRAM_API_KEY)
        self._connection_context = client.agent.v1.connect()
        self.connection = await asyncio.to_thread(self._connection_context.__enter__)
        self._deepgram_closed = False
        self.session.deepgram_connection = self.connection
        self._register_handlers(self.connection)
        await asyncio.to_thread(self.connection.send_settings,
            get_agent_settings(
                self.session.metadata,
                transport=self.session.direction,
            )
        )
        self.session.safe_transition_to(CallState.AI_ACTIVE)
        if self.metrics is not None:
            self.metrics.deepgram_started()
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
                    self._deepgram_closed = True
                    self.closing_requested = True
                    logger.warning(
                        "deepgram_keepalive_send_failed",
                        extra={"call_id": self.session.call_id, "error": str(exc)},
                    )
                    self._call_threadsafe(self.on_finished)
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
        if self._close_deadline_task is not None:
            self._close_deadline_task.cancel()
            self._close_deadline_task = None
        if self._connection_context is not None:
            with suppress(Exception):
                await asyncio.to_thread(self._connection_context.__exit__, None, None, None)
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

            if self.metrics is not None:
                # Provider-side turn boundaries and its own STT/LLM/TTS
                # split. Recorded verbatim as numbers; the end-to-end figure
                # the customer experiences is still measured at the Twilio
                # socket, because these stop at Deepgram's egress.
                if msg_type == "AgentStartedSpeaking":
                    self.metrics.agent_turn_started()
                elif msg_type == "LatencyReport":
                    # Read off the message itself. safe_event_payload() is a
                    # generic diagnostic dumper whose field list does not
                    # include the latency numbers, so routing this through it
                    # silently produced empty reports -- the whole STT/LLM/TTS
                    # breakdown was missing from real call data before this.
                    self.metrics.latency_report(message)
                elif msg_type in {"Warning", "Error"}:
                    self.metrics.provider_diagnostic(
                        msg_type.lower(),
                        getattr(message, "code", None),
                        getattr(message, "description", None),
                    )

            if msg_type == "FunctionCallRequest":
                self._handle_function_calls(message)
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
                if self.metrics is not None:
                    # Only the length crosses into metrics -- TTS is billed
                    # per character, and the text itself must never be logged.
                    if role == "assistant":
                        self.metrics.assistant_characters(len(cleaned_content))
                    else:
                        self.metrics.user_message()
                if role == "assistant" and is_terminal_assistant_text(cleaned_content):
                    self._close_after_audio_done = True
                payload = json.dumps({"role": role or "agent", "content": cleaned_content})
                self._call_threadsafe(lambda: self.on_text(payload))
        except Exception as exc:
            print(f"[deepgram] handler error for call {self.session.call_id}: {exc}")

    def _handle_function_calls(self, message) -> None:
        """Answer a Deepgram FunctionCallRequest and arm the hangup.

        Only `end_call` is registered. Anything else is answered with an
        explicit refusal rather than ignored: an unanswered client-side call
        leaves the agent waiting on a result that never arrives, which the
        customer hears as the line going dead mid-sentence.
        """
        for call in getattr(message, "functions", None) or []:
            name = str(getattr(call, "name", "") or "")
            call_id = getattr(call, "id", None)
            if not getattr(call, "client_side", True):
                # Deepgram executes it and reports back; nothing owed here.
                continue
            if name == END_CALL_FUNCTION:
                reason = normalize_end_call_reason(getattr(call, "arguments", None))
                self.session.metadata["end_call_reason"] = reason
                logger.info(
                    "agent_requested_end_call",
                    extra={"call_id": self.session.call_id, "reason": reason},
                )
                self._send_function_result(call_id, name, "Call ending.")
                # Do not hang up yet. The closing sentence is usually still
                # being synthesised; cutting now truncates the goodbye. Wait
                # for AgentAudioDone, with a bounded fallback in case the
                # model called the tool without speaking afterwards.
                self._close_after_audio_done = True
                self._arm_close_deadline()
            else:
                self._send_function_result(call_id, name, "This function is not available.")

    def _send_function_result(self, call_id: str | None, name: str, content: str) -> None:
        if self.loop is None:
            return
        response = AgentV1SendFunctionCallResponse(id=call_id, name=name, content=content)
        asyncio.run_coroutine_threadsafe(self._send_function_response(response), self.loop)

    async def _send_function_response(self, response) -> None:
        """Serialised through the same lock as every other write.

        The Deepgram SDK wraps a synchronous websocket, so two concurrent
        sends would interleave frames on one connection.
        """
        try:
            async with self._send_lock:
                if self.connection is None or self._deepgram_closed:
                    return
                await asyncio.to_thread(self.connection.send_function_call_response, response)
        except Exception as exc:
            logger.warning(
                "deepgram_function_response_failed",
                extra={"call_id": self.session.call_id, "error": type(exc).__name__},
            )

    def _arm_close_deadline(self) -> None:
        """Hang up even if AgentAudioDone never arrives.

        The whole point of the end-call tool is that a call cannot outlive
        its purpose. Making the hangup depend solely on a provider event
        would reintroduce the bug in a narrower form.
        """
        if self.loop is None or self._close_deadline_task is not None:
            return
        self._close_deadline_task = asyncio.run_coroutine_threadsafe(
            self._close_after_grace(), self.loop
        )

    async def _close_after_grace(self) -> None:
        await asyncio.sleep(AGENT_CLOSE_GRACE_SECONDS)
        if self.closing_requested:
            return
        logger.info(
            "agent_close_grace_expired",
            extra={"call_id": self.session.call_id, "grace_seconds": AGENT_CLOSE_GRACE_SECONDS},
        )
        self.closing_requested = True
        self.session.safe_transition_to(CallState.AI_FINISHED)
        self.on_finished()

    def _on_close(self, event) -> None:
        self._deepgram_closed = True
        if not self.closing_requested:
            self.closing_requested = True
            self.session.metadata["deepgram_closed"] = True
            self._call_threadsafe(self.on_finished)
        print(f"[deepgram] closed call {self.session.call_id} payload={safe_event_payload(event)}")

    @property
    def healthy(self) -> bool:
        return self.connection is not None and not self._deepgram_closed and not self.closing_requested

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
