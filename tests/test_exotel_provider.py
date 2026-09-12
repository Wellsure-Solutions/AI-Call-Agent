from __future__ import annotations

"""Exotel's control plane, and the parameter mapping that is easy to invert."""

import asyncio

import httpx
import pytest

from app.telephony.providers.base import terminal_request_for
from app.telephony.providers.exotel_provider import (
    EXOTEL_PRE_ANSWER,
    ExotelDialError,
    ExotelProvider,
    classify_exotel_error,
    normalize_status,
)
from app.storage.sqlite_store import PROVIDER_TERMINAL

ACCOUNT = "acme1"
SUCCESS_BODY = {
    "Call": {
        "Sid": "abc123def456",
        "Status": "in-progress",
        "Direction": "outbound-api",
    }
}


def provider(handler) -> tuple[ExotelProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
    return (
        ExotelProvider(
            client=client,
            account_sid=ACCOUNT,
            api_key="key-not-real",
            api_token="token-not-real",
            subdomain="api.in.exotel.com",
            caller_id="+918047000000",
        ),
        seen,
    )


def ok(payload=SUCCESS_BODY):
    return lambda request: httpx.Response(200, json=payload)


def form_fields(request: httpx.Request) -> dict[str, str]:
    from urllib.parse import parse_qsl

    return dict(parse_qsl(request.content.decode()))


# ---------------------------------------------------------------------------
# The mapping everyone gets backwards
# ---------------------------------------------------------------------------
def test_dial_puts_the_destination_in_from_and_the_exophone_in_callerid():
    """Exotel's `from` is the number being DIALLED and `callerid` is the
    ExoPhone. Twilio's to/from do not map across. Inverting these dials our
    own ExoPhone and bills for it, so this is pinned explicitly."""
    exotel, seen = provider(ok())

    asyncio.run(
        exotel.dial(
            call_id="call-1",
            to_number="+919812345678",
            ring_timeout=45,
            stream_url="wss://calls.example.invalid/exotel/media-stream?call_id=call-1",
            status_callback_url="https://calls.example.invalid/exotel/status/call-1",
        )
    )

    fields = form_fields(seen[0])
    assert fields["From"] == "+919812345678", "From is the customer, not us"
    assert fields["CallerId"] == "+918047000000", "CallerId is the ExoPhone"


def test_dial_requests_a_bidirectional_stream_at_the_agentstream_endpoint():
    exotel, seen = provider(ok())

    asyncio.run(
        exotel.dial(
            call_id="call-2", to_number="+919812345679", ring_timeout=45,
            stream_url="wss://calls.example.invalid/exotel/media-stream?call_id=call-2",
            status_callback_url="https://calls.example.invalid/exotel/status/call-2",
        )
    )

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == f"https://api.in.exotel.com/v1/Accounts/{ACCOUNT}/Calls/connect"
    fields = form_fields(request)
    assert fields["StreamType"] == "bidirectional"
    assert fields["StreamUrl"].startswith("wss://")
    assert fields["StatusCallback"].endswith("/exotel/status/call-2")


def test_dial_uses_basic_auth_with_the_api_key_and_token():
    exotel, seen = provider(ok())
    asyncio.run(exotel.dial(call_id="c", to_number="+919812345670", ring_timeout=45,
                            stream_url="wss://x/y", status_callback_url="https://x/z"))
    assert seen[0].headers["authorization"].startswith("Basic ")


def test_dial_returns_the_bound_sid_and_normalized_status():
    exotel, _ = provider(ok())
    result = asyncio.run(exotel.dial(call_id="c", to_number="+919812345671", ring_timeout=45,
                                     stream_url="wss://x/y", status_callback_url="https://x/z"))
    assert result.provider_sid == "abc123def456"
    assert result.provider_status == "in-progress"


def test_the_stream_url_is_sent_verbatim_so_its_token_survives():
    """Any mangling here and the media socket rejects every stream."""
    url = "wss://calls.example.invalid/exotel/media-stream?call_id=c&expiry=1&token=deadbeef"
    exotel, seen = provider(ok())
    asyncio.run(exotel.dial(call_id="c", to_number="+919812345672", ring_timeout=45,
                            stream_url=url, status_callback_url="https://x/z"))
    assert form_fields(seen[0])["StreamUrl"] == url


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
def test_an_xml_response_still_yields_the_sid():
    """Exotel's v1 API answers in XML unless asked otherwise."""
    body = "<TwilioResponse><Call><Sid>xml-sid-1</Sid><Status>queued</Status></Call></TwilioResponse>"
    exotel, _ = provider(lambda request: httpx.Response(200, text=body))

    result = asyncio.run(exotel.dial(call_id="c", to_number="+919812345673", ring_timeout=45,
                                     stream_url="wss://x/y", status_callback_url="https://x/z"))

    assert result.provider_sid == "xml-sid-1"
    assert result.provider_status == "queued"


def test_a_200_carrying_an_error_payload_is_ambiguous_not_rejected():
    """Indian carrier APIs return HTTP 200 with an error body routinely. The
    call may still have been placed, so this must never be treated as a
    refusal -- that would free the phone and let the queue dial again."""
    exotel, _ = provider(lambda request: httpx.Response(200, json={"status": "failure", "message": "nope"}))

    with pytest.raises(ExotelDialError) as raised:
        asyncio.run(exotel.dial(call_id="c", to_number="+919812345674", ring_timeout=45,
                                stream_url="wss://x/y", status_callback_url="https://x/z"))

    assert exotel.classify_dial_error(raised.value) == "ambiguous"


def test_a_200_with_an_unparseable_body_is_ambiguous():
    exotel, _ = provider(lambda request: httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(ExotelDialError) as raised:
        asyncio.run(exotel.dial(call_id="c", to_number="+919812345675", ring_timeout=45,
                                stream_url="wss://x/y", status_callback_url="https://x/z"))
    assert exotel.classify_dial_error(raised.value) == "ambiguous"


# ---------------------------------------------------------------------------
# classify_dial_error -- the same rule as Twilio
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_exotel_4xx_is_a_proven_rejection(status):
    exotel, _ = provider(lambda request: httpx.Response(status, json={"message": "denied"}))
    with pytest.raises(httpx.HTTPStatusError) as raised:
        asyncio.run(exotel.dial(call_id="c", to_number="+919812345676", ring_timeout=45,
                                stream_url="wss://x/y", status_callback_url="https://x/z"))
    assert exotel.classify_dial_error(raised.value) == "rejected"


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_exotel_5xx_is_ambiguous(status):
    exotel, _ = provider(lambda request: httpx.Response(status, text="upstream error"))
    with pytest.raises(httpx.HTTPStatusError) as raised:
        asyncio.run(exotel.dial(call_id="c", to_number="+919812345677", ring_timeout=45,
                                stream_url="wss://x/y", status_callback_url="https://x/z"))
    assert exotel.classify_dial_error(raised.value) == "ambiguous"


def test_a_timeout_is_ambiguous():
    def explode(request):
        raise httpx.ReadTimeout("timed out", request=request)

    exotel, _ = provider(explode)
    with pytest.raises(httpx.ReadTimeout) as raised:
        asyncio.run(exotel.dial(call_id="c", to_number="+919812345678", ring_timeout=45,
                                stream_url="wss://x/y", status_callback_url="https://x/z"))
    assert exotel.classify_dial_error(raised.value) == "ambiguous"


def test_an_unrecognised_exception_is_ambiguous():
    assert classify_exotel_error(RuntimeError("?")) == "ambiguous"
    assert classify_exotel_error(ConnectionResetError()) == "ambiguous"


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("completed", "completed"),
        ("failed", "failed"),
        ("busy", "busy"),
        ("no-answer", "no-answer"),
        ("canceled", "canceled"),
        ("cancelled", "canceled"),
        ("queued", "queued"),
        ("in-progress", "in-progress"),
        ("COMPLETED", "completed"),
        ("  Busy  ", "busy"),
    ],
)
def test_exotel_statuses_map_into_the_stores_vocabulary(raw, expected):
    assert normalize_status(raw) == expected


def test_every_terminal_mapping_lands_in_provider_terminal():
    for raw in ("completed", "failed", "busy", "no-answer", "canceled"):
        assert normalize_status(raw) in PROVIDER_TERMINAL


def test_nonterminal_statuses_stay_outside_provider_terminal():
    for raw in ("queued", "in-progress"):
        assert normalize_status(raw) not in PROVIDER_TERMINAL


def test_an_unknown_status_is_treated_as_still_running():
    """Never invent a terminal outcome. An unrecognised word must schedule
    another reconciliation attempt, not release the capacity slot."""
    for raw in ("", None, "weird-new-status", "leg1-something"):
        assert normalize_status(raw) not in PROVIDER_TERMINAL


def test_exotel_cancels_only_before_answer():
    exotel, _ = provider(ok())
    assert exotel.terminal_request_for("queued") == "canceled"
    assert exotel.terminal_request_for("in-progress") == "completed"
    # Exotel never says "ringing"; Twilio's word must not silently work here.
    assert "ringing" not in EXOTEL_PRE_ANSWER
    assert terminal_request_for("ringing", EXOTEL_PRE_ANSWER) == "completed"


# ---------------------------------------------------------------------------
# Status lookup and hangup
# ---------------------------------------------------------------------------
def test_fetch_status_normalizes_what_exotel_reports():
    exotel, seen = provider(ok({"Call": {"Sid": "s", "Status": "no-answer"}}))
    assert asyncio.run(exotel.fetch_status("s")) == "no-answer"
    assert str(seen[0].url).endswith(f"/v1/Accounts/{ACCOUNT}/Calls/s")


def test_request_terminal_deletes_the_call_resource():
    exotel, seen = provider(ok({"Call": {"Sid": "s", "Status": "completed"}}))
    asyncio.run(exotel.request_terminal("s", "completed"))
    assert seen[0].method == "DELETE"
    assert str(seen[0].url).endswith("/Calls/s")


# ---------------------------------------------------------------------------
# AMD and configuration reporting
# ---------------------------------------------------------------------------
def test_amd_is_gated_off_because_the_agentstream_dial_has_none():
    exotel, seen = provider(ok())
    assert exotel.supports_amd is False
    asyncio.run(exotel.dial(call_id="c", to_number="+919812345679", ring_timeout=45,
                            stream_url="wss://x/y", status_callback_url="https://x/z"))
    fields = form_fields(seen[0])
    assert not any("machine" in key.lower() or "amd" in key.lower() for key in fields)


def test_missing_credentials_are_reported_by_name_never_by_value():
    exotel = ExotelProvider(account_sid="", api_key="", api_token="secret-value",
                            subdomain="api.in.exotel.com", caller_id="")
    configured, missing = exotel.is_configured()

    assert configured is False
    assert "EXOTEL_ACCOUNT_SID" in missing and "EXOTEL_CALLER_ID" in missing
    assert not any("secret-value" in item for item in missing)


def test_a_fully_configured_provider_reports_its_caller_id():
    exotel, _ = provider(ok())
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("app.core.settings.PUBLIC_BASE_URL", "https://calls.example.invalid")
        configured, missing = exotel.is_configured()
    assert configured, missing
    assert exotel.caller_id() == "+918047000000"
