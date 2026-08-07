from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress

from fastapi import WebSocket, WebSocketDisconnect
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from app.core.settings import (
    AMD_ENABLED,
    AMD_MODE,
    AMD_SILENCE_TIMEOUT_MS,
    AMD_SPEECH_END_THRESHOLD_MS,
    AMD_SPEECH_THRESHOLD_MS,
    AMD_TIMEOUT_SECONDS,
    BARGE_IN_CONFIRM_MS,
    BARGE_IN_MAX_PAUSE_MS,
    BARGE_IN_VOICE_ENERGY_THRESHOLD,
    PUBLIC_BASE_URL,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_FRAME_MS,
    TWILIO_FROM_NUMBER,
)
from app.integrations.twilio_media import decode_media_payload, encode_media_payload
from app.telephony.adapters.base import BaseTelephonyAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.audio.local_vad import VoicedDurationTracker, rms_energy
from app.telephony.audio.media_dump import MediaDump
from app.telephony.audio.paced_sender import PacedSender
from app.telephony.call_session import CallSession
from app.telephony.metrics import CallMetrics

logger = logging.getLogger(__name__)

# Send a mark roughly this often while agent audio is playing, so Twilio's
# echoed-back "mark" events give a ground-truth read on whether audio is
# genuinely still playing -- not just "how much did we hand to the socket".
_MARK_EVERY_N_FRAMES = 5  # 5 * 20ms = ~100ms

class TwilioAdapter(BaseTelephonyAdapter):
    """
    Adapter for Twilio Voice + Media Streams, mirroring BrowserAdapter's
    pattern: once self.websocket is set, start() drives the whole call the
    same way BrowserAdapter.start() does (audio_bridge.start(), a task
    pumping bridge output back out, and a loop pumping inbound audio into
    audio_bridge.receive_telephony_audio()).

    The one thing Twilio does differently from a browser socket: the
    WebSocket doesn't exist yet when connect() is called (connect() only
    places the outbound call via REST). Twilio opens its WebSocket to your
    server later, on a separate route, and identifies itself only by
    call_sid inside the stream's "start" event. Durable database correlation
    bridges that gap without retaining provider objects in process memory.
    """

    def __init__(
        self,
        audio_bridge: AudioBridge | None = None,
        client=None,
        metrics: CallMetrics | None = None,
        media_dump: MediaDump | None = None,
    ) -> None:
        super().__init__()
        # Instrumentation only. Both are optional and neither influences a
        # playback, pacing, or barge-in decision anywhere in this class.
        self.metrics = metrics
        self.media_dump = media_dump
        self.from_number = TWILIO_FROM_NUMBER
        self.public_base_url = PUBLIC_BASE_URL

        self._client = client or Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        self.call_sid: str | None = None
        self.stream_sid: str | None = None
        self.websocket: WebSocket | None = None  # set once Twilio's stream connects

        self.audio_bridge = audio_bridge
        self.client_task: asyncio.Task | None = None
        self.closing_requested = False
        self.ring_timeout = 45

        # -- Soft barge-in state -------------------------------------------------
        # Twilio's own buffer-then-clear semantics (unlike a browser socket)
        # are exactly why this lives here rather than in the shared
        # AudioBridge. See AudioBridge.hard_interrupt and PacedSender.
        self._paced_sender: PacedSender | None = None
        self._vad_tracker = VoicedDurationTracker(
            energy_threshold=BARGE_IN_VOICE_ENERGY_THRESHOLD,
            frame_ms=TWILIO_FRAME_MS,
        )
        self._pause_started_at: float | None = None
        self._frames_since_pause = 0
        self._last_caller_rms = 0.0
        self._pending_marks: set[str] = set()
        self._next_mark_id = 0
        self._frames_since_mark = 0

    def attach(self, session: CallSession) -> None:
        super().attach(session)
        if self.audio_bridge is None:
            # hard_interrupt=False: Twilio pauses and confirms via local VAD
            # instead of hard-cutting on Deepgram's first UserStartedSpeaking.
            self.audio_bridge = AudioBridge(session, hard_interrupt=False)

    # ------------------------------------------------------------------
    # Outbound call placement (REST) -- audio isn't live yet after this
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        if self.session is None:
            raise RuntimeError("TwilioAdapter.connect() called before attach(session)")
        to_number = self.session.phone_number
        if not to_number:
            raise ValueError("TwilioAdapter.connect() requires session.phone_number to be set")

        create_kwargs: dict[str, object] = dict(
            to=to_number,
            from_=self.from_number,
            url=f"{self.public_base_url}/twilio/twiml/{self.session.call_id}",
            status_callback=f"{self.public_base_url}/twilio/status/{self.session.call_id}",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            timeout=self.ring_timeout,
            trim="trim-silence",
        )
        if AMD_ENABLED:
            # async_amd=True is load-bearing, not a tuning choice: without it
            # Twilio holds the call before running our TwiML until detection
            # completes, so every human answer would pay the detection delay.
            create_kwargs.update(
                machine_detection=AMD_MODE,
                async_amd="true",
                async_amd_status_callback=f"{self.public_base_url}/twilio/amd/{self.session.call_id}",
                async_amd_status_callback_method="POST",
                machine_detection_timeout=AMD_TIMEOUT_SECONDS,
                machine_detection_speech_threshold=AMD_SPEECH_THRESHOLD_MS,
                machine_detection_speech_end_threshold=AMD_SPEECH_END_THRESHOLD_MS,
                machine_detection_silence_timeout=AMD_SILENCE_TIMEOUT_MS,
            )
        call = await asyncio.to_thread(self._client.calls.create, **create_kwargs)
        self.call_sid = call.sid
        self.session.metadata["call_sid"] = self.call_sid
        logger.info("Twilio call placed: call_id=%s call_sid=%s", self.session.call_id, self.call_sid)

    async def disconnect(self) -> None:
        if self.websocket is not None:
            await self.websocket.close()

    async def hangup(self) -> None:
        self.closing_requested = True
        await self._complete_twilio_call()
        if self.websocket is not None:
            try:
                await self.websocket.close(code=1000, reason="agent_closing_call")
            except RuntimeError:
                pass

    async def answer(self) -> None:
        # Nothing to do here for Twilio: by the time start() runs, Twilio has
        # already connected the call and opened this WebSocket. This exists
        # only to mirror BrowserAdapter's call pattern inside start().
        return

    # ------------------------------------------------------------------
    # Audio I/O -- native mu-law/8000 on both Twilio and Deepgram
    # ------------------------------------------------------------------
    async def send_audio(self, mulaw_frame: bytes) -> bool:
        """Forward Deepgram's native 8 kHz mu-law output to Twilio unchanged."""
        if self.websocket is None or self.stream_sid is None:
            logger.warning("send_audio called before Twilio WebSocket is bound; dropping frame")
            return False
        payload_b64 = encode_media_payload(mulaw_frame)
        try:
            await self.websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": payload_b64},
            }))
            # Measured here rather than where the paced sender dequeues, so
            # "first agent audio byte" means the byte actually left for
            # Twilio -- pacing delay and socket backpressure included.
            if self.metrics is not None:
                self.metrics.observe_outbound(mulaw_frame)
            if self.media_dump is not None:
                self.media_dump.write_outbound(mulaw_frame)
            return True
        except (WebSocketDisconnect, RuntimeError) as exc:
            self.closing_requested = True
            logger.info(
                "Twilio stream no longer accepts outbound audio for call %s: %s",
                self.session.call_id if self.session else "unknown",
                exc,
            )
            return False

    async def receive_audio(self) -> bytes | None:
        """Reads Twilio protocol messages until an audio frame (or stream
        end) shows up. The returned 8 kHz mu-law bytes are forwarded to
        Deepgram unchanged."""
        assert self.websocket is not None
        while True:
            raw = await self.websocket.receive_text()
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "media":
                frame = decode_media_payload(msg["media"]["payload"])
                if self.metrics is not None:
                    self.metrics.observe_inbound(frame)
                if self.media_dump is not None:
                    self.media_dump.write_inbound(frame)
                return frame

            elif event == "stop":
                return None

            elif event == "mark":
                # Twilio echoes a mark back once it has actually *played*
                # the audio that preceded it -- ground truth for "is agent
                # audio genuinely still playing", not just "did we send
                # bytes to the socket". Resolve it and keep looping; this
                # isn't a caller audio frame.
                name = (msg.get("mark") or {}).get("name")
                if name is not None:
                    self._pending_marks.discard(name)
                continue

            # "connected"/"start" (already consumed by the route before
            # start() is called) and "dtmf" need no action here.

    async def clear_playback(self) -> None:
        """Not part of the base interface, but useful for barge-in: stops
        any buffered outbound audio Twilio hasn't played yet."""
        if self.websocket is not None and self.stream_sid is not None:
            await self.websocket.send_text(json.dumps({"event": "clear", "streamSid": self.stream_sid}))
            self._pending_marks.clear()

    async def _send_mark(self) -> None:
        """Sent right after handing audio to the paced sender's output so
        the ack tells us Twilio actually played roughly up to this point."""
        if self.websocket is None or self.stream_sid is None:
            return
        self._next_mark_id += 1
        name = f"m{self._next_mark_id}"
        self._pending_marks.add(name)
        try:
            await self.websocket.send_text(json.dumps({
                "event": "mark",
                "streamSid": self.stream_sid,
                "mark": {"name": name},
            }))
        except (WebSocketDisconnect, RuntimeError):
            self._pending_marks.discard(name)

    async def _on_frame_sent(self) -> None:
        self._frames_since_mark += 1
        if self._frames_since_mark >= _MARK_EVERY_N_FRAMES:
            self._frames_since_mark = 0
            await self._send_mark()

    @property
    def audio_currently_playing(self) -> bool:
        """True if Twilio still has agent audio queued or (per unresolved
        marks) actually playing -- confirmed via mark round-trips rather
        than assumed from what we've handed to the socket."""
        return bool(self._pending_marks) or (
            self._paced_sender is not None and self._paced_sender.has_buffered_audio
        )

    # ------------------------------------------------------------------
    # Call loop -- mirrors BrowserAdapter.start() exactly
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self.session is None or self.audio_bridge is None:
            raise RuntimeError("TwilioAdapter must be attached to a CallSession before start().")
        if self.websocket is None or self.stream_sid is None:
            raise RuntimeError("TwilioAdapter.start() called before the Twilio stream was bound.")

        await self.answer()
        # Start the outbound pump as soon as the Twilio stream is bound. The AI
        # still starts only after inbound media arrives, but this task must be
        # waiting before Deepgram can emit the greeting/audio; otherwise early
        # audio can sit unsent while the receive loop is busy forwarding caller
        # media.
        self.client_task = asyncio.create_task(self._send_to_twilio())
        close_status = "completed"
        try:
            while not self.closing_requested:
                audio = await self.receive_audio()
                if audio:
                    self._process_barge_in_signal(audio)
                    if not self.audio_bridge.started:
                        # Start Deepgram only after Twilio has delivered actual
                        # media from an answered call. Starting earlier can make
                        # Deepgram close with CLIENT_MESSAGE_TIMEOUT before any
                        # caller audio arrives during high-volume batches.
                        await self.audio_bridge.start()
                    accepted = await self.audio_bridge.receive_telephony_audio(audio)
                    if not accepted:
                        close_status = "ai_disconnected"
                        logger.info("AI audio bridge finished accepting Twilio audio for call %s", self.session.call_id)
                        self.closing_requested = True
                        await self._complete_twilio_call()
                        break
                else:
                    break  # Twilio sent "stop" -- call ended on the caller's side
        except WebSocketDisconnect:
            close_status = "client_disconnected"
            logger.info("Twilio stream disconnected for call %s", self.session.call_id)
        except Exception as exc:
            close_status = "error"
            logger.exception("Twilio stream error for call %s: %s", self.session.call_id, exc)
        finally:
            if close_status in {"ai_disconnected", "error"}:
                await self._complete_twilio_call()
            if self.client_task is not None:
                self.client_task.cancel()
            await self.audio_bridge.stop(close_status)

    async def stop(self) -> None:
        self.closing_requested = True
        if self.audio_bridge is not None:
            await self.audio_bridge.stop()

    async def _send_to_twilio(self) -> None:
        assert self.audio_bridge is not None
        self._paced_sender = PacedSender(self.send_audio, on_frame_sent=self._on_frame_sent)
        sender_task = asyncio.create_task(self._paced_sender.run())
        try:
            while True:
                message_type, data = await self.audio_bridge.next_output()
                if message_type == "audio" and isinstance(data, bytes):
                    # Feed the paced sender rather than sending straight to
                    # Twilio -- it re-slices this into fixed 20ms frames and
                    # drains them at real-time rate.
                    self._paced_sender.feed(data)
                elif message_type == "pause":
                    self._begin_soft_pause()
                elif message_type == "interrupt":
                    # Either AudioBridge is in hard_interrupt mode, or our
                    # own local VAD just confirmed a genuine barge-in via
                    # commit_interruption(). Either way: drop whatever
                    # hasn't been sent yet and clear whatever Twilio has
                    # already buffered.
                    self._paced_sender.discard()
                    self._end_soft_pause()
                    await self.clear_playback()
                elif message_type == "control" and isinstance(data, str):
                    self.closing_requested = True
                    await self._complete_twilio_call()
                    if self.websocket is not None:
                        try:
                            await self.websocket.close(code=1000, reason="agent_closing_call")
                        except RuntimeError:
                            pass
                    break
                # "text" (live transcript, meant for a browser UI) has no
                # destination on a phone call's audio-only WebSocket --
                # intentionally dropped here. If you want transcripts out of
                # a Twilio call, log them or push to your own event stream
                # instead of trying to send them down this socket.
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.closing_requested = True
            logger.exception("Twilio outbound audio pump failed for call %s: %s", self.session.call_id if self.session else "unknown", exc)
        finally:
            if self._paced_sender is not None:
                self._paced_sender.close()
            sender_task.cancel()
            with suppress(asyncio.CancelledError):
                await sender_task

    # ------------------------------------------------------------------
    # Soft barge-in: pace, pause, decide (via local VAD on caller audio),
    # then act. Deepgram's UserStartedSpeaking only ever *pauses* playback
    # here; committing to a real interruption is decided purely from how
    # long the caller has sustained voiced audio -- never from what
    # Deepgram thinks was said, so Hindi/Hinglish STT variance never
    # enters the decision.
    # ------------------------------------------------------------------
    def _begin_soft_pause(self) -> None:
        if self._paced_sender is None:
            return
        self._paced_sender.pause()
        self._vad_tracker.reset()
        self._pause_started_at = time.monotonic()
        self._frames_since_pause = 0
        if self.metrics is not None:
            self.metrics.barge_in_pause(self.audio_currently_playing)

    def _end_soft_pause(self) -> None:
        self._pause_started_at = None
        self._frames_since_pause = 0
        self._vad_tracker.reset()

    def _record_barge_in(self, decision: str) -> None:
        """Log why this pause ended, with the evidence that decided it.

        Every one of these is either a customer whose interruption was
        honoured or a customer who got talked over -- and from the outside
        the two are indistinguishable without the RMS, the sustained voiced
        duration, and whether agent audio was playing at the time.
        """
        if self.metrics is None or self._pause_started_at is None:
            return
        self.metrics.barge_in_decision(
            decision,
            rms=self._last_caller_rms,
            voiced_ms=self._vad_tracker.voiced_ms,
            elapsed_ms=(time.monotonic() - self._pause_started_at) * 1000,
            frames=self._frames_since_pause,
            agent_playing=self.audio_currently_playing,
        )

    def _process_barge_in_signal(self, caller_frame: bytes) -> None:
        """Called for every caller frame while a soft pause is active, to
        decide whether to resume the agent (backchannel/noise) or commit to
        a real interruption (sustained voice)."""
        if self._pause_started_at is None or self._paced_sender is None:
            return

        self._frames_since_pause += 1
        self._vad_tracker.observe(caller_frame)
        self._last_caller_rms = rms_energy(caller_frame)

        if self._vad_tracker.voiced_ms >= BARGE_IN_CONFIRM_MS:
            # Sustained voice long enough: this is a genuine interruption.
            self._record_barge_in("commit")
            self._end_soft_pause()
            self.audio_bridge.commit_interruption()
            return

        # Give the tracker's own hangover window a real chance to run
        # before treating voiced_ms == 0 as "this run of sound already
        # ended" -- otherwise a single silent frame right after the pause
        # began (before we've even had a chance to observe the caller
        # speech that triggered it) would resume instantly.
        if (
            self._vad_tracker.voiced_ms == 0
            and self._frames_since_pause >= self._vad_tracker.hangover_frames
        ):
            # Backchannel, cough, or noise: resume from exactly where the
            # ticker paused. Nothing was lost.
            self._record_barge_in("resume")
            self._end_soft_pause()
            self._paced_sender.resume()
            return

        elapsed_ms = (time.monotonic() - self._pause_started_at) * 1000
        if elapsed_ms >= BARGE_IN_MAX_PAUSE_MS:
            # Safety net: the signal has stayed ambiguous too long. Fail
            # open rather than leave the line silently paused.
            self._record_barge_in("timeout")
            self._end_soft_pause()
            self._paced_sender.resume()


    async def _complete_twilio_call(self) -> None:
        if not self.call_sid:
            return
        try:
            await asyncio.to_thread(self._client.calls(self.call_sid).update, status="completed")
        except Exception as exc:
            logger.warning("Unable to complete Twilio call %s: %s", self.call_sid, exc)

    async def fetch_status(self, call_sid: str) -> str:
        call = await asyncio.to_thread(self._client.calls(call_sid).fetch)
        return str(call.status).lower()

    async def update_status(self, call_sid: str, status: str) -> None:
        await asyncio.to_thread(self._client.calls(call_sid).update, status=status)

    # ------------------------------------------------------------------
    # TwiML builder -- used by the /twilio/twiml webhook route
    # ------------------------------------------------------------------
    @staticmethod
    def build_twiml(stream_ws_url: str, parameters: dict[str, str] | None = None) -> str:
        response = VoiceResponse()
        connect = Connect()
        stream = connect.stream(url=stream_ws_url)
        for name, value in (parameters or {}).items():
            stream.parameter(name=name, value=value)
        response.append(connect)
        return str(response)