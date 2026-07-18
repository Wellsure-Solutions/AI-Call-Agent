from __future__ import annotations

import asyncio

from app.core.conversation.conversation_engine import ConversationEngine
from app.services.call_service import CallResultService
from app.telephony.call_session import CallSession
from app.telephony.state_machine import CallState


class AudioBridge:
    """Passes adapter-formatted audio between telephony and Deepgram."""

    def __init__(self, session: CallSession, result_service: CallResultService | None = None) -> None:
        self.session = session
        self.result_service = result_service
        self.outbound_queue: asyncio.Queue[tuple[str, bytes | str]] = asyncio.Queue()
        self.finished = asyncio.Event()
        self.started = False
        self.engine = ConversationEngine(
            session=session,
            on_audio=self._queue_audio,
            on_text=self._queue_text,
            on_finished=self._mark_finished,
            on_interrupted=self._handle_interruption,
        )

    async def start(self) -> None:
        if self.started:
            return
        await self.engine.start()
        self.started = True

    async def receive_telephony_audio(self, frame: bytes) -> bool:
        return await self.engine.receive_audio(frame)

    async def close_ai(self) -> None:
        await self.engine.stop()
        self.started = False

    async def next_output(self) -> tuple[str, bytes | str]:
        return await self.outbound_queue.get()

    async def stop(self, status: str = "completed") -> None:
        await self.engine.stop()
        self.started = False
        if self.session.ended_at is None:
            if self.session.state_machine.state == CallState.AI_ACTIVE:
                self.session.safe_transition_to(CallState.AI_FINISHED)
            if self.result_service is not None:
                self.session.safe_transition_to(CallState.EXTRACTION)
                self.result_service.finalize(self.session, status)
            else:
                self.session.finish(status)

    def _queue_audio(self, frame: bytes) -> None:
        self.outbound_queue.put_nowait(("audio", frame))

    def _queue_text(self, payload: str) -> None:
        self.outbound_queue.put_nowait(("text", payload))

    def _handle_interruption(self) -> None:
        """Discard queued agent speech and tell the adapter to stop playback."""
        retained: list[tuple[str, bytes | str]] = []
        while True:
            try:
                message = self.outbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if message[0] not in {"audio", "interrupt"}:
                retained.append(message)

        self.outbound_queue.put_nowait(("interrupt", '{"event": "user_started_speaking"}'))
        for message in retained:
            self.outbound_queue.put_nowait(message)

    def _mark_finished(self) -> None:
        self.outbound_queue.put_nowait(("control", '{"event": "closing_call"}'))
        self.finished.set()