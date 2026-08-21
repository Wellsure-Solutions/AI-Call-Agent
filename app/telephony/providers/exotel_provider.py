from __future__ import annotations

"""Exotel's control plane.

`httpx` rather than an SDK: Exotel has no officially maintained Python client,
and the surface used here is three form-encoded requests.

The parameter mapping is the thing to get right and the easy thing to get
wrong. On `/v1/accounts/{sid}/calls/connect`, Exotel's `from` is **the number
being dialled** and `callerid` is the ExoPhone shown to them. That is the
opposite of what the names suggest to anyone arriving from Twilio, where `to`
is the destination and `from_` is the caller. It is consistent with Exotel's
classic "connect two numbers" API, where `From` is simply the leg dialled
first -- AgentStream has no second human leg, so `from` is the only number
dialled. Swapping them dials our own ExoPhone and bills for it.
"""

import json
import logging
from typing import Any

import httpx

from app.core.settings import (
    EXOTEL_ACCOUNT_SID,
    EXOTEL_API_KEY,
    EXOTEL_API_TOKEN,
    EXOTEL_CALLER_ID,
    EXOTEL_SUBDOMAIN,
)
from app.integrations.audio_profiles import TELEPHONY_AUDIO_PROFILE
from app.telephony.providers.base import DialErrorKind, DialResult, terminal_request_for

logger = logging.getLogger(__name__)

# Exotel's own status words. `queued` and `in-progress` are the only
# non-terminal ones; the remaining five are spelled exactly as
# `PROVIDER_TERMINAL` already spells them, so the normalization below is
# nearly identity -- but it is still done explicitly, because an unrecognised
# status must map to something non-terminal rather than fall through.
EXOTEL_STATUS_MAP = {
    "queued": "queued",
    "in-progress": "in-progress",
    "in progress": "in-progress",
    "inprogress": "in-progress",
    "completed": "completed",
    "failed": "failed",
    "busy": "busy",
    "no-answer": "no-answer",
    "noanswer": "no-answer",
    "no answer": "no-answer",
    "canceled": "canceled",
    "cancelled": "canceled",
}

# Exotel never reports `ringing`; a call it has not connected yet is `queued`.
EXOTEL_PRE_ANSWER = frozenset({"queued"})

# Anything we cannot recognise is treated as still running. That direction is
# the safe one: a call is only ever removed from capacity by a *proven*
# terminal status, so an unknown word schedules another reconciliation attempt
# instead of inventing an outcome.
UNKNOWN_STATUS = "in-progress"

CONNECT_TIMEOUT_SECONDS = 15.0


def normalize_status(raw: Any) -> str:
    """Map an Exotel status word into this codebase's vocabulary."""
    return EXOTEL_STATUS_MAP.get(str(raw or "").strip().lower(), UNKNOWN_STATUS)


def classify_exotel_error(error: Exception) -> DialErrorKind:
    """Same rule as Twilio: only a proven 4xx refusal is `rejected`.

    Everything else -- timeouts, 5xx, connection failures, an unparseable
    body, an error payload delivered with HTTP 200 -- may correspond to a call
    that really was placed, so it is ambiguous and is never redialed.
    """
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if 400 <= status < 500:
            return "rejected"
    return "ambiguous"


class ExotelDialError(RuntimeError):
    """A dial that failed in a way we cannot prove was a refusal.

    Raised for the HTTP-200-with-an-error-body case, which Indian carrier APIs
    do routinely. Deliberately *not* an httpx.HTTPStatusError, so
    `classify_exotel_error` files it as ambiguous.
    """


class ExotelProvider:
    """Exotel AgentStream control plane. Implements `TelephonyProvider`."""

    name = "exotel"
    audio_profile = TELEPHONY_AUDIO_PROFILE
    # The AgentStream connect endpoint documents no machine-detection
    # parameter and no async AMD callback. `AnsweredBy` exists elsewhere in
    # Exotel's API, but not on the dial we use, so detection is off rather
    # than silently absent. record_answered_by stays carrier-agnostic if it
    # ever appears on the status callback.
    supports_amd = False

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        account_sid: str | None = None,
        api_key: str | None = None,
        api_token: str | None = None,
        subdomain: str | None = None,
        caller_id: str | None = None,
    ) -> None:
        self._client = client
        self.account_sid = EXOTEL_ACCOUNT_SID if account_sid is None else account_sid
        self.api_key = EXOTEL_API_KEY if api_key is None else api_key
        self.api_token = EXOTEL_API_TOKEN if api_token is None else api_token
        self.subdomain = EXOTEL_SUBDOMAIN if subdomain is None else subdomain
        self._caller_id = EXOTEL_CALLER_ID if caller_id is None else caller_id

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    @property
    def _base(self) -> str:
        return f"https://{self.subdomain}/v1/accounts/{self.account_sid}"

    async def _request(self, method: str, url: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        auth = (self.api_key, self.api_token)
        if self._client is not None:
            response = await self._client.request(method, url, data=data, auth=auth)
        else:
            async with httpx.AsyncClient(timeout=CONNECT_TIMEOUT_SECONDS) as client:
                response = await client.request(method, url, data=data, auth=auth)
        response.raise_for_status()
        return self._parse_call(response)

    @staticmethod
    def _parse_call(response: httpx.Response) -> dict[str, Any]:
        """Pull the Call object out of a response that may not be JSON.

        Exotel's v1 API answers in XML by default. Rather than depend on a
        `.json` suffix the AgentStream docs do not show, this parses JSON when
        it can and falls back to lifting <Sid>/<Status> out of XML.

        A 200 whose body carries an error instead of a call is treated as
        *ambiguous*, not as a failure: Indian carrier APIs return those
        routinely, and the call may well have been placed. Never redial on it.
        """
        body = (response.text or "").strip()
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            payload = None

        if isinstance(payload, dict):
            call = payload.get("Call") or payload.get("call")
            if isinstance(call, dict):
                return call
            # A 200 carrying {"status": "failure", ...} or similar.
            raise ExotelDialError(f"no Call object in response ({sorted(payload)[:5]})")

        sid = _between(body, "<Sid>", "</Sid>")
        if sid:
            return {"Sid": sid, "Status": _between(body, "<Status>", "</Status>")}
        raise ExotelDialError("response contained no call identifier")

    # ------------------------------------------------------------------
    # Control plane
    # ------------------------------------------------------------------
    async def dial(
        self,
        *,
        call_id: str,
        to_number: str,
        ring_timeout: int,
        stream_url: str,
        status_callback_url: str,
    ) -> DialResult:
        """Place the call and bind the SID immediately.

        Unlike Twilio there is no TwiML step: the media URL (with its token)
        goes out here as `streamurl`, and the CallSid comes back in this
        response rather than on a later webhook.

        Exotel documents no ring timeout on this endpoint, so `ring_timeout`
        is enforced only by the durable ring deadline in SQLite -- which is
        where it is enforced for Twilio too; Twilio's own `timeout` was always
        belt and braces on top of it.

        `timelimit` is sent as a carrier-side backstop on total call length,
        so a runaway call stops costing money even if this process dies. It is
        not the mechanism the queue relies on: `max_call_deadline` is.
        """
        from app.core.settings import MAX_CALL_SECONDS

        payload = {
            # `from` is the number being DIALLED. See the module docstring.
            "from": to_number,
            # `callerid` is our ExoPhone, i.e. Twilio's `from_`.
            "callerid": self._caller_id,
            "streamurl": stream_url,
            "streamtype": "bidirectional",
            "statuscallback": status_callback_url,
            "statuscallbackevents[]": "terminal",
            # Exotel caps this at 14400 seconds.
            "timelimit": str(min(max(int(MAX_CALL_SECONDS), 1), 14400)),
        }
        call = await self._request("POST", f"{self._base}/calls/connect", payload)
        sid = call.get("Sid") or call.get("sid")
        status = call.get("Status") or call.get("status")
        logger.info("exotel_call_placed", extra={"call_id": call_id, "bound": bool(sid)})
        return DialResult(str(sid) if sid else None, normalize_status(status) if status else None)

    async def fetch_status(self, provider_sid: str) -> str:
        call = await self._request("GET", f"{self._base}/calls/{provider_sid}")
        return normalize_status(call.get("Status") or call.get("status"))

    async def request_terminal(self, provider_sid: str, requested: str) -> None:
        """Ask Exotel to end the call.

        Exotel exposes hangup as a DELETE on the call resource rather than a
        status update, so `requested` records intent for the caller's log but
        does not change the request -- there is only one way to end a call.
        """
        await self._request("DELETE", f"{self._base}/calls/{provider_sid}")

    def terminal_request_for(self, status: str) -> str:
        return terminal_request_for(status, EXOTEL_PRE_ANSWER)

    def classify_dial_error(self, error: Exception) -> DialErrorKind:
        return classify_exotel_error(error)

    # ------------------------------------------------------------------
    # Configuration reporting (settings UI)
    # ------------------------------------------------------------------
    def is_configured(self) -> tuple[bool, list[str]]:
        from app.core.settings import PUBLIC_BASE_URL

        missing = [
            name
            for name, value in (
                ("EXOTEL_ACCOUNT_SID", self.account_sid),
                ("EXOTEL_API_KEY", self.api_key),
                ("EXOTEL_API_TOKEN", self.api_token),
                ("EXOTEL_SUBDOMAIN", self.subdomain),
                ("EXOTEL_CALLER_ID", self._caller_id),
                ("PUBLIC_BASE_URL", PUBLIC_BASE_URL),
            )
            if not value
        ]
        return not missing, missing

    def caller_id(self) -> str:
        return self._caller_id


def _between(text: str, opening: str, closing: str) -> str:
    start = text.find(opening)
    if start < 0:
        return ""
    end = text.find(closing, start + len(opening))
    return text[start + len(opening):end].strip() if end > start else ""
