from __future__ import annotations

"""Twilio's control plane, lifted out of TwilioAdapter unchanged.

Behaviour here is a straight move, not a rewrite: the same REST kwargs, the
same `timeout` parameter, the same async-AMD wiring, and the same
rejected/ambiguous rule the coordinator used to apply inline.
"""

import asyncio
import threading
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
from app.telephony.providers.base import (
    DialErrorKind,
    DialResult,
    describe_error,
    scrub,
    terminal_request_for,
)

# Twilio's own words for "not answered yet". Asking Twilio to `complete` a
# call in one of these states is not an error, but it records the wrong
# outcome, so the distinction is kept.
TWILIO_PRE_ANSWER = frozenset({"queued", "ringing", "initiated"})

_shared_client_lock = threading.Lock()
_shared_client: Client | None = None


def get_shared_twilio_client() -> Client:
    """One Twilio REST client for the whole process.

    `twilio.rest.Client` wraps a `TwilioHttpClient`, which owns a
    `requests.Session` -- an HTTP connection pool that keeps its sockets open
    (keep-alive) after each request and that `Client` gives no way to close.
    `TwilioProvider` is deliberately constructed fresh per call (`get_provider`
    builds a new one for every dial and every reconciliation attempt, keyed on
    the call's persisted provider), and building a new `Client` to match --
    the previous behaviour of `TwilioProvider._client` -- meant a new,
    never-closed `Session` and its sockets on every single one of those calls.
    `TwilioAdapter` had the same problem, built eagerly in `__init__`.
    Credentials are read from the environment once at import time (see
    `app.core.settings`), so sharing one client for the process's lifetime
    cannot serve a stale account SID or auth token.
    """
    global _shared_client
    if _shared_client is None:
        with _shared_client_lock:
            if _shared_client is None:
                _shared_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _shared_client


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
        """Resolved on first use, not in __init__, and shared across calls.

        `is_configured()` and `caller_id()` are called on every enqueue to
        resolve the provider, and neither needs an SDK client, so touching
        `_client` there would build one per candidate per queued call for no
        reason. And because `TwilioProvider` itself is constructed fresh per
        call (see `get_provider`), building a fresh `Client` here too used to
        mean a fresh, never-closed `requests.Session` -- and its open sockets
        -- on every dial and every reconciliation attempt. `_client` now
        resolves to the one process-wide client from `get_shared_twilio_client`
        unless a test has passed its own.
        """
        if self._explicit_client is None:
            self._explicit_client = get_shared_twilio_client()
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

    def describe_dial_error(self, error: Exception) -> str:
        """The carrier's own reason, so a failed dial is diagnosable.

        Twilio's exception carries a numeric error code that maps to a
        documented cause; that plus the message is what an operator needs.
        Scrubbed with the auth token, because a `TwilioRestException`'s string
        form includes the request URI.
        """
        secrets = (TWILIO_AUTH_TOKEN, TWILIO_ACCOUNT_SID)
        if isinstance(error, TwilioRestException):
            parts = [f"HTTP {error.status} from Twilio"]
            if getattr(error, "code", None):
                parts.append(f"code {error.code}")
            if getattr(error, "msg", None):
                parts.append(str(error.msg))
            return scrub(": ".join(parts), secrets)
        return describe_error(error, secrets)

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
