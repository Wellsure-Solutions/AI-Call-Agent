from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import suppress
from dataclasses import asdict, is_dataclass
from typing import Callable

from deepgram import DeepgramClient
from deepgram.agent.v1.types import AgentV1InjectAgentMessage
from deepgram.core.events import EventType

from app.core.settings import (
    AGENT_CLOSE_UNSPOKEN_GRACE_SECONDS,
    DEEPGRAM_FALLBACK_CLOSING,
    IDLE_CLOSING_MESSAGE,
    IDLE_NUDGE_MESSAGE,
)
from app.integrations.audio_profiles import get_audio_profile
from app.integrations.deepgram.config import (
    DEEPGRAM_API_KEY,
    cached_closing_audio,
    get_agent_settings,
)
from app.services.call_control import is_closing_call_message, is_terminal_assistant_text
from app.services.transcript_sanitizer import strip_spoken_internal_commands
from app.telephony.call_session import CallSession
from app.telephony.metrics import CallMetrics
from app.telephony.state_machine import CallState

logger = logging.getLogger(__name__)

AudioCallback = Callable[[bytes], None]
TextCallback = Callable[[str], None]
FinishedCallback = Callable[[], None]
InterruptedCallback = Callable[[], None]

DEEPGRAM_KEEPALIVE_INTERVAL_SECONDS = 5


def safe_event_payload(event) -> dict[str, object]:
    if event is None:
        return {}
    if is_dataclass(event):
        return asdict(event)
    if isinstance(event, dict):
        return event

    payload: dict[str, object] = {}
    for name in (
        "type",
        "description",
        "message",
        "code",
        "variant",
        "role",
        "content",
    ):
        value = getattr(event, name, None)
        if value is not None:
            payload[name] = value

    if not payload:
        payload["repr"] = repr(event)

    return payload


class ConversationEngine:
    """Lean Deepgram conversation engine for natural seller conversations."""

    def __init__(
        self,
        session: CallSession,
        on_audio: AudioCallback,
        on_text: TextCallback,
        on_finished: FinishedCallback,
        on_interrupted: InterruptedCallback | None = None,
        metrics: CallMetrics | None = None,
        greeting_already_played: bool = False,
    ) -> None:
        self.session = session
        self.metrics = metrics
        self.greeting_already_played = greeting_already_played

        self.on_audio = on_audio
        self.on_text = on_text
        self.on_finished = on_finished
        self.on_interrupted = on_interrupted

        self.loop: asyncio.AbstractEventLoop | None = None
        self.connection = None
        self._connection_context = None

        self.closing_requested = False
        self._deepgram_closed = False
        self._close_after_audio_done = False
        self._terminal_closing_spoken = False

        self._send_lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task | None = None
        self._close_deadline_task = None

    async def start(self) -> None:
        if not DEEPGRAM_API_KEY:
            raise RuntimeError("DEEPGRAM_API_KEY is not set on the server.")

        self.loop = asyncio.get_running_loop()

        client = DeepgramClient(api_key=DEEPGRAM_API_KEY)
        self._connection_context = client.agent.v1.connect()
        self.connection = await asyncio.to_thread(
            self._connection_context.__enter__
        )

        self._deepgram_closed = False
        self.session.deepgram_connection = self.connection
        self._register_handlers(self.connection)

        await asyncio.to_thread(
            self.connection.send_settings,
            get_agent_settings(
                self.session.metadata,
                transport=self.session.direction,
                greeting_already_played=self.greeting_already_played,
            ),
        )

        self.session.safe_transition_to(CallState.AI_ACTIVE)

        if self.metrics is not None:
            self.metrics.deepgram_started()

        threading.Thread(
            target=self.connection.start_listening,
            daemon=True,
        ).start()

        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop()
        )

    async def receive_audio(self, audio_frame: bytes) -> bool:
        if (
            self.connection is None
            or self.closing_requested
            or self._deepgram_closed
            or not audio_frame
        ):
            return False

        try:
            async with self._send_lock:
                if (
                    self.connection is None
                    or self.closing_requested
                    or self._deepgram_closed
                ):
                    return False

                await asyncio.to_thread(
                    self.connection.send_media,
                    audio_frame,
                )

            return True

        except Exception as exc:
            self._deepgram_closed = True
            self.closing_requested = True
            self.session.metadata["deepgram_send_error"] = str(exc)

            if self._is_normal_deepgram_close(exc):
                logger.info(
                    "deepgram_send_media_closed",
                    extra={
                        "call_id": self.session.call_id,
                        "details": str(exc),
                    },
                )
            else:
                logger.exception(
                    "deepgram_send_media_failed",
                    extra={"call_id": self.session.call_id},
                )

            self._call_threadsafe(self.on_finished)
            return False

    async def _keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(DEEPGRAM_KEEPALIVE_INTERVAL_SECONDS)

                if (
                    self.connection is None
                    or self._deepgram_closed
                    or self.closing_requested
                ):
                    return

                try:
                    async with self._send_lock:
                        if (
                            self.connection is None
                            or self._deepgram_closed
                            or self.closing_requested
                        ):
                            return

                        await asyncio.to_thread(
                            self.connection.send_keep_alive
                        )

                except Exception as exc:
                    self._deepgram_closed = True
                    self.closing_requested = True

                    logger.warning(
                        "deepgram_keepalive_send_failed",
                        extra={
                            "call_id": self.session.call_id,
                            "error": str(exc),
                        },
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
            # A pending deadline outliving the call would fire on_finished()
            # against a session that has already been finalised.
            self._close_deadline_task.cancel()
            self._close_deadline_task = None

        if self._connection_context is not None:
            with suppress(Exception):
                await asyncio.to_thread(
                    self._connection_context.__exit__,
                    None,
                    None,
                    None,
                )
            self._connection_context = None
            self.connection = None

    def _register_handlers(self, connection) -> None:
        connection.on(
            EventType.OPEN,
            lambda _event: print(
                f"[deepgram] opened call {self.session.call_id}"
            ),
        )
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

            print(
                f"[deepgram] call={self.session.call_id} "
                f"message type={msg_type} "
                f"payload={safe_event_payload(message)}"
            )

            if msg_type == "UserStartedSpeaking":
                if self.on_interrupted is not None:
                    self._call_threadsafe(self.on_interrupted)
                return

            if self.metrics is not None:
                if msg_type == "AgentStartedSpeaking":
                    self.metrics.agent_turn_started()
                elif msg_type == "LatencyReport":
                    self.metrics.latency_report(message)
                elif msg_type in {"Warning", "Error"}:
                    self.metrics.provider_diagnostic(
                        msg_type.lower(),
                        getattr(message, "code", None),
                        getattr(message, "description", None),
                    )

            # Ignore internal/provider close hints until a real spoken terminal
            # sentence has been observed.
            if is_closing_call_message(message, content):
                if not self._terminal_closing_spoken:
                    # The model wants to hang up without having said goodbye.
                    # Ignoring the signal is right, but on its own it leaves
                    # the call open with nothing scheduled to end it -- so arm
                    # the deadline that speaks a closing for it if it stays
                    # silent. This is the path
                    # AGENT_CLOSE_UNSPOKEN_GRACE_SECONDS exists for.
                    logger.warning(
                        "ignored_premature_closing_signal",
                        extra={"call_id": self.session.call_id},
                    )
                    self._arm_close_deadline(AGENT_CLOSE_UNSPOKEN_GRACE_SECONDS)
                    return
                self._close_after_audio_done = True
                return

            if msg_type == "AgentAudioDone" and self._close_after_audio_done:
                self.closing_requested = True
                self.session.safe_transition_to(CallState.AI_FINISHED)
                self._call_threadsafe(self.on_finished)
                return

            if msg_type == "ConversationText" or (role and content):
                cleaned_content = (
                    strip_spoken_internal_commands(content)
                    if role == "assistant"
                    else (content or "")
                )

                if not cleaned_content:
                    return

                self.session.add_turn(role, cleaned_content)

                if self.metrics is not None:
                    if role == "assistant":
                        self.metrics.assistant_characters(len(cleaned_content))
                    else:
                        self.metrics.user_message()

                if (
                    role == "assistant"
                    and is_terminal_assistant_text(cleaned_content)
                ):
                    self._terminal_closing_spoken = True
                    self._close_after_audio_done = True

                payload = json.dumps(
                    {
                        "role": role or "agent",
                        "content": cleaned_content,
                    }
                )
                self._call_threadsafe(lambda: self.on_text(payload))

        except Exception as exc:
            logger.exception(
                "deepgram_message_handler_failed",
                extra={
                    "call_id": self.session.call_id,
                    "error": str(exc),
                },
            )

    # ------------------------------------------------------------------
    # Speaking on demand: nudging a quiet line, and closing one nobody is on
    # ------------------------------------------------------------------
    async def nudge_idle_customer(self) -> bool:
        """Ask the agent to check whether the seller is still there.

        Injected rather than played from a file so the model knows it said
        it: a seller who does come back finds a conversation that still makes
        sense, instead of an agent with no idea it just spoke.
        """
        return await self._inject_agent_message(IDLE_NUDGE_MESSAGE, "idle_nudge")

    async def close_for_silence(self) -> None:
        """Say goodbye to a line nobody is answering, then end the call.

        Armed before the injection, so the goodbye's own AgentAudioDone ends
        the call through the ordinary path -- the same drain that keeps a
        normal closing from being cut off. If the injection is refused there
        is no audio and no AgentAudioDone, which is what the deadline covers.
        """
        self.session.metadata.setdefault("end_call_reason", "no_response")
        self._terminal_closing_spoken = True
        self._close_after_audio_done = True
        await self._inject_agent_message(IDLE_CLOSING_MESSAGE, "idle_closing")
        self._arm_close_deadline(AGENT_CLOSE_UNSPOKEN_GRACE_SECONDS)

    async def _inject_agent_message(self, message: str, reason: str) -> bool:
        if not message or self.connection is None or self._deepgram_closed or self.closing_requested:
            return False
        try:
            async with self._send_lock:
                if self.connection is None or self._deepgram_closed:
                    return False
                await asyncio.to_thread(
                    self.connection.send_inject_agent_message,
                    AgentV1InjectAgentMessage(message=message),
                )
            logger.info(
                "agent_message_injected",
                extra={"call_id": self.session.call_id, "reason": reason},
            )
            return True
        except Exception as exc:
            # Never fatal. Deepgram refuses an injection while the agent is
            # already speaking, which is a legitimate "not now", not an error.
            logger.info(
                "agent_message_injection_failed",
                extra={
                    "call_id": self.session.call_id,
                    "reason": reason,
                    "error": type(exc).__name__,
                },
            )
            return False

    # ------------------------------------------------------------------
    # The backstop: a call must never simply stop making noise
    # ------------------------------------------------------------------
    def _arm_close_deadline(self, grace_seconds: float) -> None:
        """End the call even if the closing that was promised never arrives.

        Re-arming replaces any deadline already pending, so a later, shorter
        one is not silently skipped by an earlier one still running.
        """
        if self.loop is None:
            return
        if self._close_deadline_task is not None:
            self._close_deadline_task.cancel()
        self._close_deadline_task = asyncio.run_coroutine_threadsafe(
            self._close_after_grace(grace_seconds), self.loop
        )

    async def _close_after_grace(self, grace_seconds: float) -> None:
        await asyncio.sleep(grace_seconds)
        if self.closing_requested:
            return
        logger.info(
            "agent_close_grace_expired",
            extra={"call_id": self.session.call_id, "grace_seconds": grace_seconds},
        )
        if not self._terminal_closing_spoken:
            # Asked for a closing and produced nothing. Rather than drop the
            # line in silence -- which is what a seller experiences as being
            # hung up on -- say goodbye ourselves. Queued before on_finished()
            # so the adapter's close path drains it like any other agent audio.
            self._speak_fallback_closing()
        self.closing_requested = True
        self.session.safe_transition_to(CallState.AI_FINISHED)
        self.on_finished()

    def _speak_fallback_closing(self) -> bool:
        """Play the pre-rendered goodbye. Returns whether anything played.

        Only on transports whose wire format matches the cached audio. The
        cache is 8 kHz mu-law because that is what the phone leg carries; a
        browser session runs linear16 at 24 kHz, and pushing mu-law bytes into
        it would emit noise, not a goodbye.
        """
        if get_audio_profile(self.session.direction).encoding != "mulaw":
            return False
        audio = cached_closing_audio()
        if not audio:
            # Nothing rendered for the *current* voice. A missing cache must
            # never fail a call, but it silently costs the seller their
            # goodbye -- and the usual cause is a voice or wording change that
            # invalidated a cache nobody re-rendered, which is invisible until
            # somebody listens to a recording. Loud, and counted.
            logger.warning(
                "fallback_closing_unavailable",
                extra={
                    "call_id": self.session.call_id,
                    "remedy": "python scripts/prerender_greeting.py",
                },
            )
            if self.metrics is not None:
                self.metrics.fallback_closing_unavailable()
            return False
        logger.info("fallback_closing_spoken", extra={"call_id": self.session.call_id})
        if self.metrics is not None:
            self.metrics.fallback_closing_spoken()
        self.session.add_turn("assistant", DEEPGRAM_FALLBACK_CLOSING)
        self.on_audio(audio)
        return True

    def _on_close(self, event) -> None:
        self._deepgram_closed = True

        if not self.closing_requested:
            self.closing_requested = True
            self.session.metadata["deepgram_closed"] = True
            self._call_threadsafe(self.on_finished)

        print(
            f"[deepgram] closed call {self.session.call_id} "
            f"payload={safe_event_payload(event)}"
        )

    @property
    def healthy(self) -> bool:
        return (
            self.connection is not None
            and not self._deepgram_closed
            and not self.closing_requested
        )

    def _is_normal_deepgram_close(self, exc: Exception) -> bool:
        text = f"{type(exc).__name__}: {exc}"
        return (
            "ConnectionClosedOK" in text
            or "received 1000" in text
            or "received 1005" in text
        )

    def _on_error(self, error) -> None:
        error_payload = safe_event_payload(error)
        self.closing_requested = True
        self._deepgram_closed = True
        self.session.metadata["deepgram_error"] = error_payload

        logger.error(
            "deepgram_agent_error",
            extra={
                "call_id": self.session.call_id,
                "details": error_payload,
            },
        )

        print(
            f"[deepgram] call={self.session.call_id} "
            f"error: {error_payload}"
        )

        self._call_threadsafe(
            lambda: self.on_text(
                json.dumps(
                    {
                        "error": "Deepgram agent error",
                        "details": error_payload,
                    }
                )
            )
        )

        self._call_threadsafe(self.on_finished)

    def _call_threadsafe(self, callback: Callable[[], None]) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(callback)
