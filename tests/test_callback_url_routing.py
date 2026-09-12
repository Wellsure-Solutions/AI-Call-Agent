from __future__ import annotations

"""Every provider gets its own callback and media URLs, and nobody else's.

This file exists because `callback_urls` used to branch on `provider !=
"exotel"`. That is correct for exactly two carriers and silently wrong for a
third: Teler would have fallen into the Twilio branch and been handed Twilio's
endpoints, so its media socket and status webhook would 403 forever with
nothing in any log to say why. Twilio's own outage of that shape -- a
STREAM_SECRET that stopped being read -- dropped every call two seconds after
pickup and took a day to find.

So the property under test is not "Teler's URLs are right". It is that each
provider's URLs are right *and* distinct from every other provider's, stated in
a way that fails the moment a fourth carrier is added without extending the
branch.
"""

import time
from urllib.parse import parse_qs, urlparse

import pytest

from app.telephony.callback_urls import (
    media_stream_url,
    status_callback_url,
    teler_callback_token,
    teler_flow_token,
    teler_flow_url,
    teler_media_stream_url,
    teler_stream_token,
    valid_teler_callback_token,
    valid_teler_flow_token,
    valid_teler_stream_token,
)
from app.telephony.providers import PROVIDER_NAMES

CALL_ID = "call-routing-1"


@pytest.fixture(autouse=True)
def origin(monkeypatch):
    monkeypatch.setattr("app.telephony.callback_urls.STREAM_SECRET", "test-secret-not-real")
    monkeypatch.setattr("app.telephony.callback_urls.PUBLIC_BASE_URL", "https://calls.example.invalid")


# ---------------------------------------------------------------------------
# The branch itself
# ---------------------------------------------------------------------------
def test_every_registered_provider_gets_a_distinct_media_endpoint():
    """A provider sharing another's endpoint is either a 403 loop or, worse,
    one carrier's audio arriving at another's parser."""
    paths = {name: urlparse(media_stream_url(name, CALL_ID)).path for name in PROVIDER_NAMES}

    assert len(set(paths.values())) == len(PROVIDER_NAMES), f"endpoints collide: {paths}"


def test_every_registered_provider_gets_a_distinct_status_endpoint():
    paths = {name: urlparse(status_callback_url(name, CALL_ID)).path for name in PROVIDER_NAMES}

    assert len(set(paths.values())) == len(PROVIDER_NAMES), f"endpoints collide: {paths}"


def test_each_provider_is_routed_to_its_own_prefix():
    assert urlparse(media_stream_url("twilio", CALL_ID)).path == "/media-stream"
    assert urlparse(media_stream_url("exotel", CALL_ID)).path == "/exotel/media-stream"
    assert urlparse(media_stream_url("teler", CALL_ID)).path == "/teler/media-stream"

    assert urlparse(status_callback_url("twilio", CALL_ID)).path == f"/twilio/status/{CALL_ID}"
    assert urlparse(status_callback_url("exotel", CALL_ID)).path == f"/exotel/status/{CALL_ID}"
    assert urlparse(status_callback_url("teler", CALL_ID)).path == f"/teler/status/{CALL_ID}"


def test_an_unknown_provider_still_falls_back_to_twilio():
    """The `else` arm. Not a hazard on its own -- the registry rejects unknown
    names long before this -- but it must stay deliberate rather than become
    whichever branch happens to be last."""
    assert urlparse(media_stream_url("carrier-pigeon", CALL_ID)).path == "/media-stream"


def test_media_urls_are_websocket_scheme_and_status_urls_are_https():
    for name in PROVIDER_NAMES:
        assert media_stream_url(name, CALL_ID).startswith("wss://"), name
        assert status_callback_url(name, CALL_ID).startswith("https://"), name


# ---------------------------------------------------------------------------
# Which providers carry a token of ours, and why
# ---------------------------------------------------------------------------
def test_only_exotel_carries_a_token_on_the_dial_time_media_url():
    """Twilio and Teler both name the media URL *after* the call exists -- in
    TwiML and in the stream flow respectively -- so the dial-time URL is bare
    for both. Only Exotel has to hand it over before any id exists."""
    assert "token=" in media_stream_url("exotel", CALL_ID)
    assert "token=" not in media_stream_url("twilio", CALL_ID)
    assert "token=" not in media_stream_url("teler", CALL_ID)


def test_both_unsigned_carriers_get_a_status_token_and_twilio_does_not():
    """Twilio's callbacks are authenticated by Twilio's own signature. Exotel
    signs nothing. Teler signs, but with a dashboard secret this service cannot
    confirm is set -- so it is treated as unsigned for the control that must
    pass, and its signature is checked on top."""
    assert "token=" not in status_callback_url("twilio", CALL_ID)
    assert "token=" in status_callback_url("exotel", CALL_ID)
    assert "token=" in status_callback_url("teler", CALL_ID)


def test_the_teler_status_token_outlives_the_call():
    from app.core.settings import MAX_CALL_SECONDS

    query = parse_qs(urlparse(status_callback_url("teler", CALL_ID)).query)

    assert int(query["expiry"][0]) > time.time() + MAX_CALL_SECONDS
    assert valid_teler_callback_token(CALL_ID, int(query["expiry"][0]), query["token"][0])


def test_the_teler_flow_token_outlives_ring_time():
    """Teler fetches the flow when the call *connects*. A token that expired
    while the phone was ringing would fail the call at the worst moment."""
    from app.core.settings import RING_TIMEOUT_SECONDS

    query = parse_qs(urlparse(teler_flow_url(CALL_ID)).query)

    assert int(query["expiry"][0]) > time.time() + RING_TIMEOUT_SECONDS
    assert valid_teler_flow_token(CALL_ID, int(query["expiry"][0]), query["token"][0])


def test_the_teler_stream_token_round_trips_through_the_query_string():
    query = parse_qs(urlparse(teler_media_stream_url(CALL_ID, "cs_1")).query)

    assert query["call_id"] == [CALL_ID]
    assert valid_teler_stream_token(CALL_ID, "cs_1", int(query["expiry"][0]), query["token"][0])


# ---------------------------------------------------------------------------
# Token discipline
# ---------------------------------------------------------------------------
def test_telers_three_tokens_are_domain_separated():
    """Different lifetimes and different blast radii. A flow token is valid for
    the whole call; a stream token binds live audio. One must never validate
    for another."""
    expiry = int(time.time()) + 300
    flow = teler_flow_token(CALL_ID, expiry)
    callback = teler_callback_token(CALL_ID, expiry)
    stream = teler_stream_token(CALL_ID, "cs_1", expiry)

    assert len({flow, callback, stream}) == 3
    assert not valid_teler_flow_token(CALL_ID, expiry, callback)
    assert not valid_teler_callback_token(CALL_ID, expiry, flow)
    assert not valid_teler_stream_token(CALL_ID, "cs_1", expiry, flow)


def test_telers_tokens_are_separated_from_exotels_too():
    from app.telephony.callback_urls import (
        exotel_callback_token,
        valid_exotel_callback_token,
    )

    expiry = int(time.time()) + 300

    assert teler_callback_token(CALL_ID, expiry) != exotel_callback_token(CALL_ID, expiry)
    assert not valid_exotel_callback_token(CALL_ID, expiry, teler_callback_token(CALL_ID, expiry))
    assert not valid_teler_callback_token(CALL_ID, expiry, exotel_callback_token(CALL_ID, expiry))


def test_a_teler_stream_token_is_bound_to_the_call_the_carrier_id_and_the_expiry():
    expiry = int(time.time()) + 300
    good = teler_stream_token(CALL_ID, "cs_1", expiry)

    assert not valid_teler_stream_token("call-OTHER", "cs_1", expiry, good)
    assert not valid_teler_stream_token(CALL_ID, "cs_OTHER", expiry, good)
    assert not valid_teler_stream_token(CALL_ID, "cs_1", expiry + 1, good)
    assert not valid_teler_stream_token(CALL_ID, "cs_1", expiry, "")
    assert not valid_teler_stream_token(
        CALL_ID, "cs_1", expiry, good[:-1] + ("0" if good[-1] != "0" else "1")
    )


def test_an_expired_teler_token_is_rejected():
    expired = int(time.time()) - 1

    assert not valid_teler_flow_token(CALL_ID, expired, teler_flow_token(CALL_ID, expired))
    assert not valid_teler_callback_token(CALL_ID, expired, teler_callback_token(CALL_ID, expired))
    assert not valid_teler_stream_token(
        CALL_ID, "cs_1", expired, teler_stream_token(CALL_ID, "cs_1", expired)
    )


def test_a_blank_secret_rejects_every_teler_token(monkeypatch):
    """The failure that once dropped every call two seconds after pickup, asked
    of the third carrier. Failing closed is the right direction; failing open
    would authenticate nothing."""
    monkeypatch.setattr("app.telephony.callback_urls.STREAM_SECRET", "")
    expiry = int(time.time()) + 300

    assert teler_flow_token(CALL_ID, expiry) == ""
    assert teler_stream_token(CALL_ID, "cs_1", expiry) == ""
    assert not valid_teler_flow_token(CALL_ID, expiry, "")
    assert not valid_teler_flow_token(CALL_ID, expiry, "anything")
    assert not valid_teler_stream_token(CALL_ID, "cs_1", expiry, "anything")
    assert not valid_teler_callback_token(CALL_ID, expiry, "anything")
