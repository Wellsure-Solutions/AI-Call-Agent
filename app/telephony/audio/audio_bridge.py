from __future__ import annotations

import asyncio

from app.core.conversation.conversation_engine import ConversationEngine
from app.services.call_service import CallResultService
from app.telephony.call_session import CallSession
from app.telephony.state_machine import CallState


class AudioBridge:
    """Passes adapter-formatted audio between telephony and Deepgram.

    `hard_interrupt` controls what happens the instant Deepgram reports
    UserStartedSpeaking:

    * True (default, used by BrowserAdapter) -- the original hard-cut
      behavior. Buffered agent audio is discarded and playback is cleared
      immediately. Fine for a browser test call, which has none of
      Twilio's buffer-then-clear semantics to worry about.
    * False (used by TwilioAdapter) -- nothing is discarded yet. A "pause"
      message is queued instead, and it's up to the adapter to actually
      commit (via `commit_interruption`) once it has confirmed, using its
      own local VAD, that this is a real interruption and not a
      backchannel/cough/noise. That confirmation logic belongs in the
      adapter, not here, since it depends on Twilio-specific transport
      semantics (buffer-then-clear) a browser socket doesn't have.
    """

    def __init__(
        self,
        session: CallSession,
        result_service: CallResultService | None = None,
        hard_interrupt: bool = True,
    ) -> None:
        self.session = session
        self.result_service = result_service
        self.hard_interrupt = hard_interrupt
        self.outbound_queue: asyncio.Queue[tuple[str, bytes | str]] = asyncio.Queue()
        self.finished = asyncio.Event()
        self._started = False
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
        self._started = True

    @property
    def started(self) -> bool:
        return self._started and self.engine.healthy

    async def receive_telephony_audio(self, frame: bytes) -> bool:
        return await self.engine.receive_audio(frame)

    async def close_ai(self) -> None:
        await self.engine.stop()
        self._started = False

    async def next_output(self) -> tuple[str, bytes | str]:
        return await self.outbound_queue.get()

    async def stop(self, status: str = "completed") -> None:
        await self.engine.stop()
        self._started = False
        if self.session.ended_at is None:
            if self.session.state_machine.state == CallState.AI_ACTIVE:
                self.session.safe_transition_to(CallState.AI_FINISHED)
            if self.result_service is not None:
                self.session.safe_transition_to(CallState.EXTRACTION)
                await self.result_service.afinalize(self.session, status)
            else:
                self.session.finish(status)

    def _queue_audio(self, frame: bytes) -> None:
        self.outbound_queue.put_nowait(("audio", frame))

    def _queue_text(self, payload: str) -> None:
        self.outbound_queue.put_nowait(("text", payload))

    def _handle_interruption(self) -> None:
        """Fired the instant Deepgram reports UserStartedSpeaking."""
        if self.hard_interrupt:
            self._discard_and_clear()
            return
        # Soft mode: don't touch buffered/queued agent audio yet. Just tell
        # the adapter a candidate interruption has begun so it can pause
        # playback while its own local VAD confirms this is a real
        # interruption rather than a backchannel/cough/noise.
        self.outbound_queue.put_nowait(("pause", '{"event": "user_started_speaking"}'))

    def commit_interruption(self) -> None:
        """For soft-interrupt adapters: call this once local VAD confirms
        sustained caller speech, to actually discard buffered agent audio
        and tell the adapter to clear whatever the transport has already
        buffered."""
        self._discard_and_clear()

    def _discard_and_clear(self) -> None:
        """Discard queued agent speech and tell the adapter to stop playback."""
        retained: list[tuple[str, bytes | str]] = []
        while True:
            try:
                message = self.outbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if message[0] not in {"audio", "interrupt", "pause"}:
                retained.append(message)

        self.outbound_queue.put_nowait(("interrupt", '{"event": "user_started_speaking"}'))
        for message in retained:
            self.outbound_queue.put_nowait(message)

    def _mark_finished(self) -> None:
        self.outbound_queue.put_nowait(("control", '{"event": "closing_call"}'))
        self.finished.set()