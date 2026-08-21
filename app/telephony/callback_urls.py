from __future__ import annotations

"""Per-call callback and media URLs, and the tokens that authenticate them.

One module rather than two so that the code minting a token and the code
verifying it cannot drift apart -- the failure mode when they do is silent
(every callback 403s, no call produces media) and has already cost this
project an outage once.

Twilio and Exotel need different things here, for a structural reason:

  Twilio  learns the media URL *after* the call exists. It fetches signed
          TwiML from /twilio/twiml/{call_id}, and the stream token is minted
          there over (call_id, CallSid, expiry) because by then the CallSid
          is known. Callbacks are authenticated by Twilio's own signature.

  Exotel  is told the media URL *at dial time*, before any SID exists, and
          signs nothing. So the token goes in the query string and cannot
          cover the SID, and status callbacks need a token of our own.

See `exotel_stream_token` for why dropping the SID from the token does not
weaken correlation.
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
# URL construction
# ---------------------------------------------------------------------------
def media_stream_url(provider: str, call_id: str, now: int | None = None) -> str:
    """The websocket URL handed to the carrier.

    Twilio gets the bare endpoint: it is not used at dial time at all, and the
    real URL (with token) is rendered into TwiML later.

    Exotel gets call_id, expiry and token as query parameters, which it echoes
    back in the start event's `custom_parameters`. Exotel permits at most 3
    custom parameters totalling under 256 characters; this uses exactly 3 and
    ~133 characters, so the full SHA-256 digest fits without truncation.
    """
    base = websocket_base()
    if provider != "exotel":
        return f"{base}/media-stream"
    expiry = int(now if now is not None else time.time()) + STREAM_TOKEN_TTL_SECONDS
    token = exotel_stream_token(call_id, expiry)
    return f"{base}/exotel/media-stream?call_id={call_id}&expiry={expiry}&token={token}"


def status_callback_url(provider: str, call_id: str, now: int | None = None) -> str:
    """The status webhook URL handed to the carrier.

    Twilio's is unauthenticated by us and validated by signature on arrival.
    Exotel's carries an HMAC query token because Exotel signs nothing.
    """
    if provider != "exotel":
        return f"{PUBLIC_BASE_URL}/twilio/status/{call_id}"
    expiry = int(now if now is not None else time.time()) + CALLBACK_TOKEN_TTL_SECONDS
    token = exotel_callback_token(call_id, expiry)
    return f"{PUBLIC_BASE_URL}/exotel/status/{call_id}?expiry={expiry}&token={token}"
