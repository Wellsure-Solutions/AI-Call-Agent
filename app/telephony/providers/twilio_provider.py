from __future__ import annotations

"""Twilio's control plane, lifted out of TwilioAdapter unchanged.

Behaviour here is a straight move, not a rewrite: the same REST kwargs, the
same `timeout` parameter, the same async-AMD wiring, and the same
rejected/ambiguous rule the coordinator used to apply inline.
"""

import asyncio
from typing import Any

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from app.core.settings import (
    AMD_ENABLED,
    AMD_MODE,
    AMD_SILENCE_TIMEOUT_MS,
    AMD_SPEECH_END_THRESHOLD_MS,
    AMD_SPEECH_THRESHOLD_MS,
    AMD_TIMEOUT_SECONDS,
    PUBLIC_BASE_URL,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_FROM_NUMBER,
)
from app.integrations.audio_profiles import TELEPHONY_AUDIO_PROFILE
from app.telephony.providers.base import DialErrorKind, DialResult, terminal_request_for

# Twilio's own words for "not answered yet". Asking Twilio to `complete` a
# call in one of these states is not an error, but it records the wrong
# outcome, so the distinction is kept.
TWILIO_PRE_ANSWER = frozenset({"queued", "ringing", "initiated"})


def build_call_kwargs(
    *,
    call_id: str,
    to_number: str,
    from_number: str,
    public_base_url: str,
    ring_timeout: int,
    amd_enabled: bool,
) -> dict[str, Any]:
    """The exact kwargs `client.calls.create` has always been given.

    `amd_enabled` is passed in rather than read from settings here so that
    both entry points -- the provider and TwilioAdapter.connect() -- observe
    the same flag their own module sees.
    """
    create_kwargs: dict[str, Any] = dict(
        to=to_number,
        from_=from_number,
        url=f"{public_base_url}/twilio/twiml/{call_id}",
        status_callback=f"{public_base_url}/twilio/status/{call_id}",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        timeout=ring_timeout,
        trim="trim-silence",
    )
    if amd_enabled:
        # async_amd=True is load-bearing, not a tuning choice: without it
        # Twilio holds the call before running our TwiML until detection
        # completes, so every human answer would pay the detection delay.
        create_kwargs.update(
            machine_detection=AMD_MODE,
            async_amd="true",
            async_amd_status_callback=f"{public_base_url}/twilio/amd/{call_id}",
            async_amd_status_callback_method="POST",
            machine_detection_timeout=AMD_TIMEOUT_SECONDS,
            machine_detection_speech_threshold=AMD_SPEECH_THRESHOLD_MS,
            machine_detection_speech_end_threshold=AMD_SPEECH_END_THRESHOLD_MS,
            machine_detection_silence_timeout=AMD_SILENCE_TIMEOUT_MS,
        )
    return create_kwargs


def classify_twilio_error(error: Exception) -> DialErrorKind:
    """A 4xx from Twilio is a refusal it never acted on; everything else --
    timeouts, 5xx, connection resets, anything unrecognised -- may have placed
    a real call, so it is ambiguous and is never redialed."""
    status = getattr(error, "status", None)
    if isinstance(error, TwilioRestException) and isinstance(status, int) and 400 <= status < 500:
        return "rejected"
    return "ambiguous"


class TwilioProvider:
    """Twilio Voice control plane. Implements `TelephonyProvider`."""

    name = "twilio"
    audio_profile = TELEPHONY_AUDIO_PROFILE
    supports_amd = True

    def __init__(self, client=None, from_number: str | None = None, public_base_url: str | None = None) -> None:
        self._explicit_client = client
        self.from_number = TWILIO_FROM_NUMBER if from_number is None else from_number
        self.public_base_url = PUBLIC_BASE_URL if public_base_url is None else public_base_url

    @property
    def _client(self):
        """Built on first use, not in __init__.

        `is_configured()` and `caller_id()` are called on every enqueue to
        resolve the provider, and neither needs an SDK client. Constructing
        one there would set up an HTTP client per candidate per queued call,
        and would make configuration reporting depend on the SDK tolerating
        empty credentials in its constructor -- which it does today, and need
        not tomorrow.
        """
        if self._explicit_client is None:
            self._explicit_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        return self._explicit_client

    # ------------------------------------------------------------------
    # Control plane
    # ------------------------------------------------------------------
    async def dial(
        self,
        *,
        call_id: str,
        to_number: str,
        ring_timeout: int,
        stream_url: str = "",
        status_callback_url: str = "",
        amd_enabled: bool | None = None,
    ) -> DialResult:
        """Place the call.

        `stream_url` is accepted and ignored: Twilio does not learn the media
        URL at dial time. It fetches signed TwiML from `/twilio/twiml/{call_id}`
        after the call is created, and the stream token is minted there against
        the CallSid Twilio reports. Exotel is the opposite -- see ExotelProvider.
        """
        create_kwargs = build_call_kwargs(
            call_id=call_id,
            to_number=to_number,
            from_number=self.from_number,
            public_base_url=self.public_base_url,
            ring_timeout=ring_timeout,
            amd_enabled=AMD_ENABLED if amd_enabled is None else amd_enabled,
        )
        call = await asyncio.to_thread(self._client.calls.create, **create_kwargs)
        status = getattr(call, "status", None)
        return DialResult(getattr(call, "sid", None), str(status).lower() if status else None)

    async def fetch_status(self, provider_sid: str) -> str:
        call = await asyncio.to_thread(self._client.calls(provider_sid).fetch)
        return str(call.status).lower()

    async def request_terminal(self, provider_sid: str, requested: str) -> None:
        await asyncio.to_thread(self._client.calls(provider_sid).update, status=requested)

    def terminal_request_for(self, status: str) -> str:
        return terminal_request_for(status, TWILIO_PRE_ANSWER)

    def classify_dial_error(self, error: Exception) -> DialErrorKind:
        return classify_twilio_error(error)

    # ------------------------------------------------------------------
    # Configuration reporting (settings UI)
    # ------------------------------------------------------------------
    def is_configured(self) -> tuple[bool, list[str]]:
        missing = [
            name
            for name, value in (
                ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
                ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
                ("TWILIO_FROM_NUMBER", self.from_number),
                ("PUBLIC_BASE_URL", self.public_base_url),
            )
            if not value
        ]
        return not missing, missing

    def caller_id(self) -> str:
        return self.from_number

    # ------------------------------------------------------------------
    # TwiML -- used by the /twilio/twiml webhook route
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
