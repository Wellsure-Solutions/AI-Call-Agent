from __future__ import annotations

import asyncio

from app.core.conversation.conversation_engine import ConversationEngine
from app.services.call_service import CallResultService
from app.telephony.call_session import CallSession
from app.telephony.state_machine import CallState


class AudioBridge:
    """Normalizes adapter audio into PCM for the conversation engine and back."""

    def __init__(self, session: CallSession, result_service: CallResultService | None = None) -> None:
        self.session = session
        self.result_service = result_service
        self.outbound_queue: asyncio.Queue[tuple[str, bytes | str]] = asyncio.Queue()
        self.finished = asyncio.Event()
        self.engine = ConversationEngine(
            session=session,
            on_audio=self._queue_audio,
            on_text=self._queue_text,
            on_finished=self._mark_finished,
        )

    async def start(self) -> None:
        await self.engine.start()

    async def receive_telephony_audio(self, frame: bytes) -> None:
        await self.engine.receive_audio(self._normalize_to_pcm(frame))

    async def next_output(self) -> tuple[str, bytes | str]:
        return await self.outbound_queue.get()

    async def stop(self, status: str = "completed") -> None:
        await self.engine.stop()
        if self.session.ended_at is None:
            if self.session.state_machine.state == CallState.AI_ACTIVE:
                self.session.safe_transition_to(CallState.AI_FINISHED)
            if self.result_service is not None:
                self.session.safe_transition_to(CallState.EXTRACTION)
                self.result_service.finalize(self.session, status)
            else:
                self.session.finish(status)

    def _normalize_to_pcm(self, frame: bytes) -> bytes:
        # Browser and Deepgram already exchange compatible PCM bytes today.
        # Future adapters can subclass/compose codecs here without touching AI logic.
        return frame

    def _queue_audio(self, frame: bytes) -> None:
        self.outbound_queue.put_nowait(("audio", frame))

    def _queue_text(self, payload: str) -> None:
        self.outbound_queue.put_nowait(("text", payload))

    def _mark_finished(self) -> None:
        self.outbound_queue.put_nowait(("control", '{"event": "closing_call"}'))
        self.finished.set()
