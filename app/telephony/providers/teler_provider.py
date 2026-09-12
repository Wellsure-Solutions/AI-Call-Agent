from __future__ import annotations

"""FreJun Teler's control plane.

Architecturally this is the Twilio pattern, not the Exotel one. The dial
carries a `flow_url`; Teler POSTs to it once the call connects and expects a
JSON *action* back, and it is that response -- not the dial -- which names the
media socket. So the media URL is minted after the call exists, against an
identifier the carrier has already given us, exactly as `/twilio/twiml` does.
Exotel is the odd one out: only it needs the stream URL before the dial.

`httpx` rather than the official `teler` SDK, having read the SDK's source
(0.2.2) rather than its README. Three reasons, in order of weight:

  * It covers one of the three control-plane methods this Protocol requires.
    `CallResourceManager.PATHS` contains only `create`; `retrieve()` and
    `delete()` raise `NotImplementedException`, so `fetch_status()` has no
    SDK path. There is no hangup method at all -- the real endpoint is
    `POST /voice/calls/{id}/hangup` with a JSON body, which the SDK's
    id-only `delete()` shape cannot express. Two of three would be raw httpx
    anyway, and one carrier reachable through two HTTP stacks with two auth
    paths is worse than one.

  * `BaseResource.__init__` raises `TypeError` on any response key it does not
    declare. FreJun adding a field to `CallInitiateData` -- a backward
    compatible change every API makes -- would turn every dial into an
    exception. It would be filed `ambiguous` and so would not cause a double
    call, but every call would land in NEEDS_RECONCILIATION holding its
    capacity slot, which at the default concurrency is a full queue stall.

  * The request it builds is one JSON POST with an `x-api-key` header. The
    dependency buys nothing that is not written out below.

The SDK remains the reference for the request *shape*, and the wire constants
below are pinned against it and against `https://api.frejun.ai/openapi.json`
in tests/test_teler_wire_format.py.

Unlike Exotel there is no name inversion to get wrong: Teler's `from_number` is
ours and `to_number` is the destination, as on Twilio.
"""

import json
import logging
import re
from typing import Any

import httpx

from app.core.settings import (
    TELER_API_KEY,
    TELER_BASE_URL,
    TELER_CHUNK_MS,
    TELER_FROM_NUMBER,
    TELER_RECORD,
)
from app.integrations.audio_profiles import TELEPHONY_AUDIO_PROFILE
from app.telephony.providers.base import (
    DialErrorKind,
    DialResult,
    describe_error,
    scrub,
    terminal_request_for,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wire format.
#
# Paths and field names come from the SDK source and from Teler's published
# OpenAPI document, and are pinned byte-for-byte in
# tests/test_teler_wire_format.py. Exotel's casing bug is the precedent: the
# spelling in a vendor's developer guide is not evidence that the spelling
# works, and a drift here fails every outbound call at once while looking
# exactly like a credentials problem.
#
# Everything is lowercase snake_case here, and the auth header is `x-api-key`
# (HTTP headers are case-insensitive, but this is the spelling both the SDK and
# the OpenAPI security scheme use).
# ---------------------------------------------------------------------------
INITIATE_PATH = "/voice/calls/initiate"
CALLS_PATH = "/voice/calls"
HANGUP_SUFFIX = "/hangup"

AUTH_HEADER = "x-api-key"

FIELD_FROM = "from_number"        # our virtual number, i.e. Twilio's `from_`
FIELD_TO = "to_number"            # the destination, i.e. Twilio's `to`
FIELD_FLOW_URL = "flow_url"
FIELD_STATUS_CALLBACK = "status_callback_url"
FIELD_RECORD = "record"

# The stream flow returned from /teler/flow/{call_id}. `CallFlow.stream()` in
# the SDK builds `{action, ws_url, chunk_size, record}`; `sample_rate` is
# absent there but documented and used by both of FreJun's own reference
# bridges, so it is sent explicitly rather than left to a default.
FLOW_ACTION = "action"
FLOW_STREAM = "stream"
FLOW_WS_URL = "ws_url"
FLOW_CHUNK_SIZE = "chunk_size"
FLOW_SAMPLE_RATE = "sample_rate"
FLOW_RECORD = "record"

# Teler expresses this as "8k"/"16k", not as a number. 8k is the only value
# compatible with TELEPHONY_AUDIO_PROFILE: everything above the socket in this
# repo is 8 kHz mu-law, and asking for 16k would put the greeting cache, the
# barge-in RMS constants and the metrics on a different clock.
SAMPLE_RATE_8K = "8k"

# Teler's stated bounds on `chunk_size`, in milliseconds.
CHUNK_MS_ALIGNMENT = 20
CHUNK_MS_MINIMUM = 20
CHUNK_MS_MAXIMUM = 2000


def aligned_chunk_ms(requested: int) -> int:
    """Clamp and round a chunk size to something Teler will accept.

    Teler rejects a flow whose `chunk_size` is out of range or not a multiple
    of 20, and a rejected flow is a call that connects and then plays nothing.
    """
    value = max(CHUNK_MS_MINIMUM, min(int(requested), CHUNK_MS_MAXIMUM))
    aligned = (value // CHUNK_MS_ALIGNMENT) * CHUNK_MS_ALIGNMENT
    return max(aligned, CHUNK_MS_MINIMUM)

# Teler's own lifecycle words, from the CallSessionState enum. There are five,
# and none of them is `busy`, `no-answer` or `canceled` -- Teler reports those
# as a `failed` call carrying a `reason`, which is why `normalize_status` takes
# both.
TELER_STATUS_MAP = {
    "initiated": "queued",
    "ringing": "ringing",
    "answered": "in-progress",
    "completed": "completed",
    "failed": "failed",
}

# Webhook event names map onto the same five states. Kept as its own table
# rather than derived by stripping the `call.` prefix, so a new event name
# cannot silently become a status by accident.
TELER_EVENT_MAP = {
    "call.initiated": "queued",
    "call.ringing": "ringing",
    "call.answered": "in-progress",
    "call.completed": "completed",
    "call.failed": "failed",
}

# Refinements applied *only* to a failed call. A completed call carries reasons
# like `callee_hangup` and must stay `completed`: that is a real conversation
# that happened, and rewriting it from its hangup reason would misreport every
# successful call in the batch.
TELER_FAILURE_REASON_MAP = {
    "no_answer": "no-answer",
    "noanswer": "no-answer",
    "no-answer": "no-answer",
    "user_busy": "busy",
    "busy": "busy",
    "canceled": "canceled",
    "cancelled": "canceled",
}

# Same rule as Exotel: a status we do not recognise is treated as still
# running, so an unknown word schedules another reconciliation attempt instead
# of inventing a terminal outcome and releasing the phone for a redial.
UNKNOWN_STATUS = "in-progress"

# Teler reports `ringing` in its own right, so unlike Exotel there are two
# pre-answer words.
TELER_PRE_ANSWER = frozenset({"queued", "ringing"})

REQUEST_TIMEOUT_SECONDS = 15.0

# Teler's hangup takes an optional machine-readable reason, constrained to
# `^[A-Z0-9_]+$`. Sending one makes an agent-initiated hangup distinguishable
# from a customer hangup in FreJun's own console.
HANGUP_REASON = "AGENT_HANGUP"


def normalize_status(raw: Any, reason: Any = None) -> str:
    """Map a Teler lifecycle word into this codebase's vocabulary.

    `reason` refines a *failure* into the specific terminal word this codebase
    already uses, so an unanswered Teler call reports `no-answer` rather than a
    flat `failed` and reads the same as the equivalent Twilio or Exotel call in
    every report downstream. An unrecognised reason leaves `failed` alone --
    still terminal, still correct, just less specific.
    """
    status = TELER_STATUS_MAP.get(str(raw or "").strip().lower().replace("-", "_"), UNKNOWN_STATUS)
    if status == "failed":
        return TELER_FAILURE_REASON_MAP.get(str(reason or "").strip().lower(), "failed")
    return status


def normalize_event(event: Any, reason: Any = None) -> str | None:
    """Map a webhook event name onto a status, or None if it is not one.

    Returning None matters: Teler emits `stream.initiated`, `stream.completed`,
    `recording.completed` and `recording.failed` to the same URL, and
    `stream.completed` explicitly "does not imply call.completed". Treating one
    of those as a call status would terminalize a live call -- hanging up on a
    customer mid-sentence and freeing their number for a redial.
    """
    status = TELER_EVENT_MAP.get(str(event or "").strip().lower())
    if status is None:
        return None
    if status == "failed":
        return TELER_FAILURE_REASON_MAP.get(str(reason or "").strip().lower(), "failed")
    return status


def classify_teler_error(error: Exception) -> DialErrorKind:
    """Only a proven 4xx refusal is `rejected`.

    Teler documents 400, 403 and 422 on the initiate endpoint -- all decided
    before anything was dialled -- and 502 and 504, which are an upstream
    carrier that may well have placed the call. Timeouts, connection failures,
    a 2xx whose body carries no call id, and anything unrecognised are
    ambiguous for the same reason and are never redialed.
    """
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if 400 <= status < 500:
            return "rejected"
    return "ambiguous"


class TelerDialError(RuntimeError):
    """A dial whose success we cannot prove.

    Raised when Teler answers 2xx but the body carries no call id. Deliberately
    *not* an `httpx.HTTPStatusError`, so `classify_teler_error` files it as
    ambiguous: a 202 means Teler accepted the call, and a call we accepted but
    cannot name may still be ringing somebody's phone.

    Carries the raw body so `describe_dial_error` can scrub and store it,
    unscrubbed here so that redaction happens in one place next to the secrets
    it has to redact.
    """

    def __init__(self, message: str, body: str = "") -> None:
        super().__init__(message)
        self.body = body


class TelerProvider:
    """FreJun Teler control plane. Implements `TelephonyProvider`."""

    name = "teler"
    audio_profile = TELEPHONY_AUDIO_PROFILE
    # Teler's initiate endpoint documents no machine-detection parameter and
    # emits no answering-machine verdict on any webhook. Detection is off
    # rather than silently absent; `record_answered_by` stays carrier-agnostic
    # if one ever appears.
    supports_amd = False

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        from_number: str | None = None,
        base_url: str | None = None,
        record: bool | None = None,
    ) -> None:
        self._client = client
        self.api_key = TELER_API_KEY if api_key is None else api_key
        self.from_number = TELER_FROM_NUMBER if from_number is None else from_number
        self.base_url = (TELER_BASE_URL if base_url is None else base_url).rstrip("/")
        self.record = TELER_RECORD if record is None else record

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """One JSON request, returning the parsed body.

        The body is sent as pre-serialised JSON with an explicit content type,
        matching what the SDK does, so a field ordering or a `None` never turns
        into a form encoding by accident.
        """
        url = f"{self.base_url}{path}"
        headers = {
            AUTH_HEADER: self.api_key,
            "content-type": "application/json",
            "accept": "application/json",
        }
        content = json.dumps(payload) if payload is not None else None
        if self._client is not None:
            response = await self._client.request(method, url, content=content, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.request(method, url, content=content, headers=headers)
        response.raise_for_status()
        return _parse_json_object(response)

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
    ) -> DialResult:
        """Place the call and bind Teler's call id immediately.

        `stream_url` is accepted and ignored, for Twilio's reason: Teler does
        not learn the media URL at dial time. It POSTs to `flow_url` once the
        call connects, and the media URL -- with a stream token that covers
        Teler's own call id -- is minted there. Exotel is the opposite; see
        ExotelProvider.

        `ring_timeout` is likewise not sent: Teler's initiate endpoint takes no
        ring-timeout parameter. It is enforced by the durable ring deadline in
        SQLite, which is where it is really enforced for Twilio too.

        Teler answers 202 -- accepted and queued, not answered -- so the status
        reported here is `queued` rather than anything read off the body. The
        body carries no status field at all.
        """
        payload = {
            FIELD_FROM: self.from_number,
            FIELD_TO: to_number,
            FIELD_FLOW_URL: self._flow_url(call_id),
            FIELD_STATUS_CALLBACK: status_callback_url,
            FIELD_RECORD: bool(self.record),
        }
        _debug_dial_request(call_id, payload)
        body = await self._request("POST", INITIATE_PATH, payload)
        _debug_dial_response(call_id, body)
        data = body.get("data")
        if not isinstance(data, dict):
            raise TelerDialError("no call object in the response", body=json.dumps(body)[:2000])
        sid = data.get("id")
        if not sid:
            raise TelerDialError("response contained no call identifier", body=json.dumps(body)[:2000])
        status = data.get("state") or data.get("status")
        logger.info("teler_call_placed", extra={"call_id": call_id, "bound": True})
        return DialResult(str(sid), normalize_status(status) if status else "queued")

    @staticmethod
    def _flow_url(call_id: str) -> str:
        # Imported here rather than at module scope because callback_urls reads
        # PUBLIC_BASE_URL and STREAM_SECRET at import time, and this module is
        # imported by the registry on every configuration check.
        from app.telephony.callback_urls import teler_flow_url

        return teler_flow_url(call_id)

    async def fetch_status(self, provider_sid: str) -> str:
        call = await self._request("GET", f"{CALLS_PATH}/{provider_sid}")
        return normalize_status(call.get("state") or call.get("status"), call.get("reason"))

    async def request_terminal(self, provider_sid: str, requested: str) -> None:
        """Ask Teler to end the call.

        Teler exposes hangup as its own action rather than as a status update,
        so `requested` records the caller's intent for their log but does not
        change the request -- as with Exotel, there is only one way to end a
        call. Sent with no `leg_id`, which ends the whole call rather than one
        leg.
        """
        await self._request(
            "POST",
            f"{CALLS_PATH}/{provider_sid}{HANGUP_SUFFIX}",
            {"reason": HANGUP_REASON},
        )

    def terminal_request_for(self, status: str) -> str:
        return terminal_request_for(status, TELER_PRE_ANSWER)

    def classify_dial_error(self, error: Exception) -> DialErrorKind:
        return classify_teler_error(error)

    def describe_dial_error(self, error: Exception) -> str:
        """What actually went wrong, in a form an operator can act on.

        Without this every Teler failure lands on the call row as
        `HTTPStatusError`, and an unprovisioned virtual number, a revoked API
        key, a flow URL Teler could not reach and an upstream carrier outage
        all look identical.

        Teler's error envelope puts the useful part in `message` and a stable
        machine-readable `code` its own docs say to branch on, so both are
        captured. Everything goes through `scrub()` with this provider's API
        key: the body may echo the request, and the request contains our
        `flow_url` and `status_callback_url` -- each of which carries a live
        HMAC token.
        """
        secrets = (self.api_key,)
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            detail = _error_detail(error.response)
            return scrub(
                f"HTTP {status} from Teler: {detail}" if detail else f"HTTP {status} from Teler",
                secrets,
            )
        if isinstance(error, TelerDialError):
            body = scrub(error.body, secrets)
            reason = scrub(str(error), secrets)
            return scrub(
                f"Teler accepted but {reason}: {body}" if body else f"Teler accepted but {reason}",
                secrets,
            )
        return describe_error(error, secrets)

    # ------------------------------------------------------------------
    # Configuration reporting (settings UI)
    # ------------------------------------------------------------------
    def is_configured(self) -> tuple[bool, list[str]]:
        """Which named settings are missing. Never their values."""
        from app.core.settings import PUBLIC_BASE_URL

        missing = [
            name
            for name, value in (
                ("TELER_API_KEY", self.api_key),
                ("TELER_FROM_NUMBER", self.from_number),
                ("TELER_BASE_URL", self.base_url),
                ("PUBLIC_BASE_URL", PUBLIC_BASE_URL),
            )
            if not value
        ]
        return not missing, missing

    def caller_id(self) -> str:
        return self.from_number

    # ------------------------------------------------------------------
    # Call flow -- used by the /teler/flow webhook route
    # ------------------------------------------------------------------
    @staticmethod
    def build_stream_flow(
        ws_url: str,
        chunk_ms: int | None = None,
        record: bool = False,
        sample_rate: str = SAMPLE_RATE_8K,
    ) -> dict[str, Any]:
        """The JSON Teler expects back from `flow_url`.

        Teler's analogue of `TwilioProvider.build_twiml`, and it lives beside
        the dial for the same reason: one module owns everything this carrier
        is told about a call.

        `chunk_size` is in **milliseconds**, not bytes -- Teler requires a
        multiple of 20 between 20 and 2000, and rounding happens here so a
        misconfigured setting produces slightly different latency rather than a
        rejected flow and a dead call.
        """
        return {
            FLOW_ACTION: FLOW_STREAM,
            FLOW_WS_URL: ws_url,
            FLOW_CHUNK_SIZE: aligned_chunk_ms(TELER_CHUNK_MS if chunk_ms is None else chunk_ms),
            FLOW_SAMPLE_RATE: sample_rate,
            FLOW_RECORD: bool(record),
        }


# ===========================================================================
# TEMPORARY DEBUG INSTRUMENTATION -- remove with the block in teler_routes.py.
#
# A real call rang, was answered, went silent and hung up, and Teler never
# fetched flow_url at all -- no request for it reached the server, while
# status callbacks from the same dial arrived fine. Since both URLs are sent
# in the same request body and carry identical constraints in Teler's schema,
# the next thing to establish is what we actually put in that body and what
# Teler said back.
#
# `warning` with the payload inline, for the reason set out in
# teler_routes.py: nothing configures logging here, so `info` emits nothing
# and `extra=` fields are dropped.
# ===========================================================================
_TOKEN_VALUE = re.compile(r"((?:^|[?&])(?:token|expiry)=)([^&\s]+)")


def _debug_url(url: str) -> str:
    """A URL with its HMAC replaced by its length.

    The host and path are the diagnostic content -- whether the URL is
    absolute, and whether it points where we think. The token is a live
    credential and never belongs in a log, and its length alone answers the
    only question worth asking about it.
    """
    return _TOKEN_VALUE.sub(lambda m: f"{m.group(1)}<{len(m.group(2))} chars>", str(url))


def _debug_dial_request(call_id: str, payload: dict[str, Any]) -> None:
    """The dial body as sent, so flow_url can be compared against the one that
    demonstrably works -- status_callback_url, built by the same code."""
    logger.warning(
        "TELER_DEBUG dial_request call_id=%r from=%r to=%r record=%r "
        "flow_url=%s status_callback_url=%s",
        call_id, payload.get(FIELD_FROM), payload.get(FIELD_TO), payload.get(FIELD_RECORD),
        _debug_url(payload.get(FIELD_FLOW_URL) or ""),
        _debug_url(payload.get(FIELD_STATUS_CALLBACK) or ""),
    )


def _debug_dial_response(call_id: str, body: dict[str, Any]) -> None:
    """Teler's 202 body, including the id we are about to bind.

    Its format is the thing to look at: a `cs_`-prefixed id here against a raw
    UUID in the webhooks would confirm the identifier split between Teler's
    versioned and unversioned surfaces.
    """
    try:
        rendered = json.dumps(body)[:1000]
    except (TypeError, ValueError):
        rendered = repr(body)[:1000]
    logger.warning("TELER_DEBUG dial_response call_id=%r body=%s", call_id, rendered)


# =========================== END TEMPORARY DEBUG ===========================


def _parse_json_object(response: httpx.Response) -> dict[str, Any]:
    """Parse a JSON object body, or raise something classified as ambiguous.

    A 2xx that is not a JSON object is not evidence the call failed -- Teler
    already said it accepted it -- so this must never surface as an
    `HTTPStatusError`, which `classify_teler_error` would read as a proven
    refusal and release the phone for a redial.

    An empty 2xx body is a successful mutation with nothing to report, not a
    parse failure: the hangup returns 202 and this must not turn every
    agent-initiated hangup into a logged warning. `dial()` still fails
    correctly on one, because an empty object carries no call id.
    """
    text = (response.text or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as error:
        raise TelerDialError("response was not JSON", body=text[:2000]) from error
    if not isinstance(payload, dict):
        raise TelerDialError("response was not a JSON object", body=text[:2000])
    return payload


def _error_detail(response: httpx.Response) -> str:
    """Pull `message` and `code` out of Teler's error envelope.

    Falls back to the raw body: an error from a proxy in front of Teler will
    not be in Teler's envelope at all, and that body is still the only thing
    that says what happened.
    """
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return (response.text or "").strip()
    if not isinstance(payload, dict):
        return (response.text or "").strip()
    parts = [str(payload.get("message") or "").strip()]
    code = payload.get("code")
    if code:
        parts.append(f"code {code}")
    detail = ": ".join(part for part in parts if part)
    return detail or (response.text or "").strip()
