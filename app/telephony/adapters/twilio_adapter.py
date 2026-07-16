from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging
from typing import ClassVar

from fastapi import WebSocket, WebSocketDisconnect
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from app.core.settings import PUBLIC_BASE_URL, TWILIO_FROM_NUMBER
from app.telephony.adapters.base import BaseTelephonyAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.call_session import CallSession

logger = logging.getLogger(__name__)

# Twilio's Media Streams format is fixed by Twilio, not configurable.
TWILIO_SAMPLE_RATE = 8000

# NOTE: these match app/integrations/deepgram/config.py's AgentV1Settings.
# Deepgram's input and output rates are DIFFERENT from each other -- input
# is what the agent listens on, output is what its TTS produces.
DEEPGRAM_INPUT_SAMPLE_RATE = 48000   # linear16, what we must send TO Deepgram
DEEPGRAM_OUTPUT_SAMPLE_RATE = 24000  # linear16, what Deepgram sends back to us
PCM_SAMPLE_WIDTH = 2  # linear16 = 2 bytes/sample, same on every leg (Twilio included)


def _convert_deepgram_pcm_to_twilio_mulaw(pcm_frame: bytes) -> bytes:
    resampled, _ = audioop.ratecv(
        pcm_frame, PCM_SAMPLE_WIDTH, 1, DEEPGRAM_OUTPUT_SAMPLE_RATE, TWILIO_SAMPLE_RATE, None
    )
    return audioop.lin2ulaw(resampled, PCM_SAMPLE_WIDTH)


def _convert_twilio_mulaw_to_deepgram_pcm(mulaw_bytes: bytes) -> bytes:
    linear = audioop.ulaw2lin(mulaw_bytes, PCM_SAMPLE_WIDTH)
    pcm_frame, _ = audioop.ratecv(
        linear, PCM_SAMPLE_WIDTH, 1, TWILIO_SAMPLE_RATE, DEEPGRAM_INPUT_SAMPLE_RATE, None
    )
    return pcm_frame


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
    call_sid inside the stream's "start" event. TwilioAdapter._pending
    bridges that gap -- see app/telephony/twilio_routes.py for the routes
    that use it.
    """

    _pending: ClassVar[dict[str, "TwilioAdapter"]] = {}

    def __init__(self, audio_bridge: AudioBridge | None = None) -> None:
        super().__init__()
        self.from_number = TWILIO_FROM_NUMBER
        self.public_base_url = PUBLIC_BASE_URL

        self._client = Client()
        self.call_sid: str | None = None
        self.stream_sid: str | None = None
        self.websocket: WebSocket | None = None  # set once Twilio's stream connects

        self.audio_bridge = audio_bridge
        self.client_task: asyncio.Task | None = None
        self.closing_requested = False

    def attach(self, session: CallSession) -> None:
        super().attach(session)
        if self.audio_bridge is None:
            self.audio_bridge = AudioBridge(session)

    # ------------------------------------------------------------------
    # Outbound call placement (REST) -- audio isn't live yet after this
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        if self.session is None:
            raise RuntimeError("TwilioAdapter.connect() called before attach(session)")
        to_number = self.session.phone_number
        if not to_number:
            raise ValueError("TwilioAdapter.connect() requires session.phone_number to be set")

        call = await asyncio.to_thread(
            self._client.calls.create,
            to=to_number,
            from_=self.from_number,
            url=f"{self.public_base_url}/twilio/twiml",
            status_callback=f"{self.public_base_url}/twilio/status",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            trim="trim-silence",
        )
        self.call_sid = call.sid
        self.session.metadata["call_sid"] = self.call_sid
        TwilioAdapter._pending[self.call_sid] = self
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
    # Audio I/O -- mulaw/8000 (Twilio) <-> linear16/48000|24000 (Deepgram)
    # ------------------------------------------------------------------
    async def send_audio(self, pcm_frame: bytes) -> None:
        """pcm_frame arrives here as linear16 @ DEEPGRAM_OUTPUT_SAMPLE_RATE
        (that's what AudioBridge forwards from Deepgram's TTS output)."""
        if self.websocket is None or self.stream_sid is None:
            logger.warning("send_audio called before Twilio WebSocket is bound; dropping frame")
            return
        loop = asyncio.get_running_loop()
        # Offload per-frame audio conversion so one active call cannot block
        # Uvicorn's single event loop and delay unrelated coroutines like new
        # WebSocket handshakes.
        mulaw_bytes = await loop.run_in_executor(
            None, _convert_deepgram_pcm_to_twilio_mulaw, pcm_frame
        )
        payload_b64 = base64.b64encode(mulaw_bytes).decode("ascii")
        await self.websocket.send_text(json.dumps({
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": payload_b64},
        }))

    async def receive_audio(self) -> bytes | None:
        """Reads Twilio protocol messages until an audio frame (or stream
        end) shows up. Returns linear16 @ DEEPGRAM_INPUT_SAMPLE_RATE, since
        that's what ConversationEngine.receive_audio() feeds straight to
        Deepgram via connection.send_media()."""
        assert self.websocket is not None
        while True:
            raw = await self.websocket.receive_text()
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "media":
                mulaw_bytes = base64.b64decode(msg["media"]["payload"])
                loop = asyncio.get_running_loop()
                # Offload per-frame audio conversion so one active call cannot block
                # Uvicorn's single event loop and delay unrelated coroutines like new
                # WebSocket handshakes.
                return await loop.run_in_executor(
                    None, _convert_twilio_mulaw_to_deepgram_pcm, mulaw_bytes
                )

            elif event == "stop":
                return None

            # "connected"/"start" (already consumed by the route before
            # start() is called) and "mark"/"dtmf" need no action here.

    async def clear_playback(self) -> None:
        """Not part of the base interface, but useful for barge-in: stops
        any buffered outbound audio Twilio hasn't played yet."""
        if self.websocket is not None and self.stream_sid is not None:
            await self.websocket.send_text(json.dumps({"event": "clear", "streamSid": self.stream_sid}))

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
                    if not self.audio_bridge.started:
                        # Start Deepgram only after Twilio has delivered actual
                        # media from an answered call. Starting earlier can make
                        # Deepgram close with CLIENT_MESSAGE_TIMEOUT before any
                        # caller audio arrives during high-volume batches.
                        await self.audio_bridge.start()
                    accepted = await self.audio_bridge.receive_telephony_audio(audio)
                    if not accepted:
                        close_status = "ai_disconnected"
                        logger.warning("AI audio bridge stopped accepting Twilio audio for call %s", self.session.call_id)
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
            if self.client_task is not None:
                self.client_task.cancel()
            await self.audio_bridge.stop(close_status)

    async def stop(self) -> None:
        self.closing_requested = True
        if self.audio_bridge is not None:
            await self.audio_bridge.stop()

    async def _send_to_twilio(self) -> None:
        assert self.audio_bridge is not None
        try:
            while True:
                message_type, data = await self.audio_bridge.next_output()
                if message_type == "audio" and isinstance(data, bytes):
                    await self.send_audio(data)
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


    async def _complete_twilio_call(self) -> None:
        if not self.call_sid:
            return
        try:
            await asyncio.to_thread(self._client.calls(self.call_sid).update, status="completed")
        except Exception as exc:
            logger.warning("Unable to complete Twilio call %s: %s", self.call_sid, exc)

    # ------------------------------------------------------------------
    # TwiML builder -- used by the /twilio/twiml webhook route
    # ------------------------------------------------------------------
    @staticmethod
    def build_twiml(stream_ws_url: str) -> str:
        response = VoiceResponse()
        connect = Connect()
        connect.stream(url=stream_ws_url)
        response.append(connect)
        return str(response)