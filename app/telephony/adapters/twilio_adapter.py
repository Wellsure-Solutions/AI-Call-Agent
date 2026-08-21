from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocketDisconnect
from twilio.rest import Client

from app.core.settings import (
    AMD_ENABLED,
    PUBLIC_BASE_URL,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_FROM_NUMBER,
)
from app.integrations.twilio_media import decode_media_payload, encode_media_payload
from app.telephony.adapters.streaming_media import StreamingMediaAdapter
from app.telephony.audio.audio_bridge import AudioBridge
from app.telephony.audio.local_vad import rms_energy
from app.telephony.audio.media_dump import MediaDump
from app.telephony.metrics import CallMetrics
from app.telephony.providers.twilio_provider import build_call_kwargs

logger = logging.getLogger(__name__)


class TwilioAdapter(StreamingMediaAdapter):
    """
    Adapter for Twilio Voice + Media Streams.

    Everything that decides *when* to speak, pause, interrupt, drain or hang
    up lives in StreamingMediaAdapter and is shared with Exotel. What is left
    here is the wire: Twilio's camelCase `streamSid`, its base64 mu-law
    payloads, and its REST hangup.

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
        super().__init__(audio_bridge=audio_bridge, metrics=metrics, media_dump=media_dump)
        self.from_number = TWILIO_FROM_NUMBER
        self.public_base_url = PUBLIC_BASE_URL
        self._client = client or Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    # ------------------------------------------------------------------
    # Outbound call placement (REST) -- audio isn't live yet after this
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """Place the outbound call.

        The REST body is built by `TwilioProvider` -- the coordinator dials
        through the provider, and this remains the adapter-shaped entry point
        so a caller holding an adapter can still place a call. Both go through
        the same builder so the two cannot drift.

        `AMD_ENABLED` is read here, from this module, and passed down
        explicitly rather than re-read inside the provider: this is the flag
        this module's callers see and override.
        """
        if self.session is None:
            raise RuntimeError("TwilioAdapter.connect() called before attach(session)")
        to_number = self.session.phone_number
        if not to_number:
            raise ValueError("TwilioAdapter.connect() requires session.phone_number to be set")

        create_kwargs = build_call_kwargs(
            call_id=self.session.call_id,
            to_number=to_number,
            from_number=self.from_number,
            public_base_url=self.public_base_url,
            ring_timeout=self.ring_timeout,
            amd_enabled=AMD_ENABLED,
        )
        call = await asyncio.to_thread(self._client.calls.create, **create_kwargs)
        self.call_sid = call.sid
        self.session.metadata["call_sid"] = self.call_sid
        logger.info("Twilio call placed: call_id=%s call_sid=%s", self.session.call_id, self.call_sid)

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
            self._recent_agent_rms.append(rms_energy(mulaw_frame))
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
                playing = self.audio_currently_playing
                energy = rms_energy(frame)
                # The floor must be learned continuously, not only while a
                # pause is active -- by the time a barge-in candidate opens,
                # the threshold has to already be right.
                self._noise_floor.observe(energy, playing)
                # Reuses the energy already computed above rather than a
                # second pass over the frame; this runs on every inbound
                # frame for the whole call.
                await self._observe_idle(energy >= self._noise_floor.threshold, playing)
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

    async def _request_provider_hangup(self) -> None:
        await asyncio.to_thread(self._client.calls(self.call_sid).update, status="completed")

    # ------------------------------------------------------------------
    # Retained names
    # ------------------------------------------------------------------
    # The pump and the hangup are shared with Exotel and live in
    # StreamingMediaAdapter under carrier-neutral names. These aliases keep
    # the Twilio-specific spellings working for existing callers and tests.
    async def _send_to_twilio(self) -> None:
        await self._run_outbound_pump()

    async def _complete_twilio_call(self) -> None:
        await self._complete_provider_call()
