from __future__ import annotations

"""Per-call callback and media URLs, and the tokens that authenticate them.

One module rather than two so that the code minting a token and the code
verifying it cannot drift apart -- the failure mode when they do is silent
(every callback 403s, no call produces media) and has already cost this
project an outage once.

The three carriers need different things here, for a structural reason:

  Twilio  learns the media URL *after* the call exists. It fetches signed
          TwiML from /twilio/twiml/{call_id}, and the stream token is minted
          there over (call_id, CallSid, expiry) because by then the CallSid
          is known. Callbacks are authenticated by Twilio's own signature.

  Exotel  is told the media URL *at dial time*, before any SID exists, and
          signs nothing. So the token goes in the query string and cannot
          cover the SID, and status callbacks need a token of our own.

  Teler   is Twilio-shaped: the dial carries a `flow_url`, Teler POSTs to it
          once the call connects, and the *flow response* names the media
          socket. By then Teler's call id is known, so the stream token can
          cover it exactly as Twilio's covers the CallSid. What differs from
          Twilio is that FreJun document no signature on the flow request, so
          the flow URL carries a token of our own -- and their status
          callbacks, though signed, are signed with a dashboard-set secret we
          cannot verify is present, so those carry one too.

See `exotel_stream_token` for why dropping the SID from the token does not
weaken correlation -- and note that Teler does not have to make that trade.

The provider branching below is written as an explicit three-way `if/elif/else`
rather than as a boolean. It used to read `if provider != "exotel"`, which was
correct for exactly two carriers and silently wrong for a third: Teler would
have fallen into the Twilio branch and been handed Twilio's endpoints, so its
media socket and status webhook would 403 forever with no error anywhere. That
is the outage class the first paragraph is about.
"""

import hashlib
import hmac
import time

from app.core.settings import MAX_CALL_SECONDS, PUBLIC_BASE_URL, STREAM_SECRET

# How long a media-stream token stays valid. The stream is opened seconds
# after the dial, so this only has to survive ring time.
STREAM_TOKEN_TTL_SECONDS = 300

# A status callback can arrive at any point up to the end of the call, so its
# token must outlive the whole call rather than reuse the media TTL. The
# margin covers a carrier retrying a callback after the call ended.
CALLBACK_TOKEN_TTL_SECONDS = MAX_CALL_SECONDS + 3600


def websocket_base() -> str:
    return PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")


# ---------------------------------------------------------------------------
# Exotel media-stream token
# ---------------------------------------------------------------------------
def exotel_stream_token(call_id: str, expiry: int) -> str:
    """HMAC over (call_id, expiry) -- deliberately *not* over the CallSid.

    Twilio's stream token covers the CallSid because the TwiML webhook already
    knows it. Exotel's stream URL has to be built before the dial request is
    even sent, so at minting time no SID exists to cover.

    That halves what this token proves, and the other half is restored in the
    media route rather than dropped:

      * this token proves the URL came from us and has not expired;
      * the database proves the stream belongs to this call -- the route
        checks the `call_sid` in Exotel's start event against the SID durably
        bound to `call_id` at dial time, and `claim_media` then re-checks it
        inside a conditional UPDATE that also enforces single ownership.

    Both must pass. Do not "simplify" this by trusting the token alone; on its
    own it would let a replayed URL bind a different call's audio. Equally, do
    not add the SID back here -- it is not knowable at mint time.
    """
    if not STREAM_SECRET:
        return ""
    material = f"exotel-stream:{call_id}:{expiry}".encode()
    return hmac.new(STREAM_SECRET.encode(), material, hashlib.sha256).hexdigest()


def valid_exotel_stream_token(call_id: str, expiry: int, token: str) -> bool:
    return bool(
        STREAM_SECRET
        and expiry >= int(time.time())
        and hmac.compare_digest(exotel_stream_token(call_id, expiry), token)
    )


# ---------------------------------------------------------------------------
# Exotel status-callback token
# ---------------------------------------------------------------------------
def exotel_callback_token(call_id: str, expiry: int) -> str:
    """Exotel does not sign its callbacks, so we authenticate them ourselves.

    Domain-separated from the stream token by the prefix: the two have
    different lifetimes and different blast radii, and a token minted for one
    must never validate for the other.
    """
    if not STREAM_SECRET:
        return ""
    material = f"exotel-callback:{call_id}:{expiry}".encode()
    return hmac.new(STREAM_SECRET.encode(), material, hashlib.sha256).hexdigest()


def valid_exotel_callback_token(call_id: str, expiry: int, token: str) -> bool:
    return bool(
        STREAM_SECRET
        and expiry >= int(time.time())
        and hmac.compare_digest(exotel_callback_token(call_id, expiry), token)
    )


# ---------------------------------------------------------------------------
# Teler tokens
# ---------------------------------------------------------------------------
# Teler's flow request and its media stream happen at different times and prove
# different things, so they get separate, domain-separated tokens -- the same
# discipline as Exotel's stream/callback split, for the same reason: a token
# minted for one must never validate for the other.
def teler_flow_token(call_id: str, expiry: int) -> str:
    """Authenticates Teler's POST to /teler/flow/{call_id}.

    FreJun document a signature on *webhooks*; they document nothing about the
    flow request, whose body is just `{call_id, account_id, from_number,
    to_number, direction}`. An unauthenticated flow endpoint would hand a media
    URL -- and with it a live stream token -- to anyone who guessed a call_id,
    so this is minted at dial time and checked on arrival.

    Covers (call_id, expiry) only. Teler's own call id is not knowable when the
    dial request is being built, which is the same constraint Exotel's stream
    token has; it is bound instead by the database check in the flow route.
    """
    if not STREAM_SECRET:
        return ""
    material = f"teler-flow:{call_id}:{expiry}".encode()
    return hmac.new(STREAM_SECRET.encode(), material, hashlib.sha256).hexdigest()


def valid_teler_flow_token(call_id: str, expiry: int, token: str) -> bool:
    return bool(
        STREAM_SECRET
        and expiry >= int(time.time())
        and hmac.compare_digest(teler_flow_token(call_id, expiry), token)
    )


def teler_stream_token(call_id: str, teler_call_id: str, expiry: int) -> str:
    """HMAC over (call_id, Teler's call id, expiry).

    Unlike Exotel's, this *can* cover the carrier's identifier: it is minted in
    the flow route, which Teler only reaches after the call exists and which
    receives the id in its request body. So Teler gets Twilio's stronger token
    rather than Exotel's weaker one, and the media route's database check is
    reinforcement rather than the only thing binding the stream to the call.
    """
    if not STREAM_SECRET:
        return ""
    material = f"teler-stream:{call_id}:{teler_call_id}:{expiry}".encode()
    return hmac.new(STREAM_SECRET.encode(), material, hashlib.sha256).hexdigest()


def valid_teler_stream_token(call_id: str, teler_call_id: str, expiry: int, token: str) -> bool:
    return bool(
        STREAM_SECRET
        and expiry >= int(time.time())
        and hmac.compare_digest(teler_stream_token(call_id, teler_call_id, expiry), token)
    )


def teler_callback_token(call_id: str, expiry: int) -> str:
    """Authenticates Teler's status webhooks.

    Teler *does* sign its webhooks -- HMAC-SHA256 over "{timestamp}.{body}" --
    but with a secret configured per Voice App in the FreJun dashboard, which
    this service has no way to confirm is set or correct until a live call
    fails. Depending on it alone would mean either rejecting every callback
    (total, silent outage) or accepting every callback (no authentication) on a
    misconfiguration, with nothing to distinguish the two.

    So the token below is the control that must pass, and the signature is
    verified on top of it when TELER_WEBHOOK_SECRET is set. See
    `teler_routes._valid_callback`.
    """
    if not STREAM_SECRET:
        return ""
    material = f"teler-callback:{call_id}:{expiry}".encode()
    return hmac.new(STREAM_SECRET.encode(), material, hashlib.sha256).hexdigest()


def valid_teler_callback_token(call_id: str, expiry: int, token: str) -> bool:
    return bool(
        STREAM_SECRET
        and expiry >= int(time.time())
        and hmac.compare_digest(teler_callback_token(call_id, expiry), token)
    )


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------
def media_stream_url(provider: str, call_id: str, now: int | None = None) -> str:
    """The websocket URL handed to the carrier.

    Exotel gets call_id, expiry and token as query parameters, which it echoes
    back in the start event's `custom_parameters`. Exotel permits at most 3
    custom parameters totalling under 256 characters; this uses exactly 3 and
    ~133 characters, so the full SHA-256 digest fits without truncation.

    Teler gets its bare endpoint, for Twilio's reason: the media URL is not
    part of the dial at all. It is rendered into the *stream flow* returned
    from /teler/flow/{call_id}, once Teler's call id is known and can be
    covered by the token. It gets its own endpoint rather than sharing
    Twilio's, because Teler's messages are `{"type": "audio", ...}` where
    Twilio's are `{"event": "media", ...}` -- and the parser that tells those
    apart is what decides whose audio this is.

    Twilio gets the bare shared endpoint, likewise unused at dial time.
    """
    base = websocket_base()
    if provider == "exotel":
        expiry = int(now if now is not None else time.time()) + STREAM_TOKEN_TTL_SECONDS
        token = exotel_stream_token(call_id, expiry)
        return f"{base}/exotel/media-stream?call_id={call_id}&expiry={expiry}&token={token}"
    if provider == "teler":
        return f"{base}/teler/media-stream"
    return f"{base}/media-stream"


def teler_media_stream_url(call_id: str, teler_call_id: str, now: int | None = None) -> str:
    """The tokened media URL, built in the flow route rather than at dial time.

    This is Teler's equivalent of the URL Twilio's TwiML webhook renders, and
    it is minted at the same point in the call's life for the same reason: it
    is the first moment the carrier's own identifier for the call exists.
    """
    expiry = int(now if now is not None else time.time()) + STREAM_TOKEN_TTL_SECONDS
    token = teler_stream_token(call_id, teler_call_id, expiry)
    return (
        f"{websocket_base()}/teler/media-stream"
        f"?call_id={call_id}&expiry={expiry}&token={token}"
    )


def teler_flow_url(call_id: str, now: int | None = None) -> str:
    """The call-flow URL handed to Teler at dial time.

    Given the callback TTL rather than the stream TTL: Teler fetches this when
    the call *connects*, which is after ring time, and a token that expired
    while the phone was ringing would fail the call at the worst moment.
    """
    expiry = int(now if now is not None else time.time()) + CALLBACK_TOKEN_TTL_SECONDS
    token = teler_flow_token(call_id, expiry)
    return f"{PUBLIC_BASE_URL}/teler/flow/{call_id}?expiry={expiry}&token={token}"


def status_callback_url(provider: str, call_id: str, now: int | None = None) -> str:
    """The status webhook URL handed to the carrier.

    Twilio's is unauthenticated by us and validated by signature on arrival.
    Exotel's carries an HMAC query token because Exotel signs nothing. Teler's
    carries one too -- it signs, but with a secret we cannot prove is
    configured; see `teler_callback_token`.
    """
    if provider == "exotel":
        expiry = int(now if now is not None else time.time()) + CALLBACK_TOKEN_TTL_SECONDS
        token = exotel_callback_token(call_id, expiry)
        return f"{PUBLIC_BASE_URL}/exotel/status/{call_id}?expiry={expiry}&token={token}"
    if provider == "teler":
        expiry = int(now if now is not None else time.time()) + CALLBACK_TOKEN_TTL_SECONDS
        token = teler_callback_token(call_id, expiry)
        return f"{PUBLIC_BASE_URL}/teler/status/{call_id}?expiry={expiry}&token={token}"
    return f"{PUBLIC_BASE_URL}/twilio/status/{call_id}"
