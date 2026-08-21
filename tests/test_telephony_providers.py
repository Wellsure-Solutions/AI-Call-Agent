from __future__ import annotations

"""The provider control plane, and the dial-error rule it now owns.

`classify_dial_error` decides between `mark_dial_rejected` and
`mark_dial_ambiguous`. Getting it wrong in the "rejected" direction frees the
phone for a redial on a call that may really have been placed -- i.e. calls a
customer twice. Every provider is held to the same rule here.
"""

import asyncio

import pytest
from twilio.base.exceptions import TwilioRestException

from app.telephony.providers import DEFAULT_PROVIDER, PROVIDER_NAMES, get_provider
from app.telephony.providers.base import DialResult, terminal_request_for
from app.telephony.providers.twilio_provider import TwilioProvider


class FakeCalls:
    def __init__(self, sid: str = "CA" + "0" * 32) -> None:
        self.created: dict = {}
        self._sid = sid

    def create(self, **kwargs):
        self.created = kwargs
        return type("Call", (), {"sid": self._sid, "status": "queued"})()


class FakeClient:
    def __init__(self) -> None:
        self.calls = FakeCalls()


def twilio_provider() -> tuple[TwilioProvider, FakeClient]:
    client = FakeClient()
    provider = TwilioProvider(
        client=client, from_number="+15550001111", public_base_url="https://calls.example.invalid"
    )
    return provider, client


# ---------------------------------------------------------------------------
# Dial
# ---------------------------------------------------------------------------
def test_twilio_dial_places_the_call_and_returns_the_sid():
    provider, client = twilio_provider()

    result = asyncio.run(
        provider.dial(
            call_id="call-1",
            to_number="+919000000001",
            ring_timeout=45,
            stream_url="wss://calls.example.invalid/media-stream",
            status_callback_url="https://calls.example.invalid/twilio/status/call-1",
        )
    )

    assert result.provider_sid == "CA" + "0" * 32
    assert client.calls.created["to"] == "+919000000001"
    assert client.calls.created["from_"] == "+15550001111"
    assert client.calls.created["timeout"] == 45
    assert client.calls.created["url"].endswith("/twilio/twiml/call-1")


def test_twilio_ignores_the_stream_url_because_it_fetches_twiml_later():
    """Twilio learns the media URL from signed TwiML after the call exists.
    Passing it at dial time is meaningless and must not leak into the request
    -- the token in it would then be sitting in Twilio's call log."""
    provider, client = twilio_provider()

    asyncio.run(
        provider.dial(
            call_id="call-2",
            to_number="+919000000002",
            ring_timeout=30,
            stream_url="wss://calls.example.invalid/media-stream?token=secret",
            status_callback_url="https://calls.example.invalid/twilio/status/call-2",
        )
    )

    assert not any("secret" in str(value) for value in client.calls.created.values())


# ---------------------------------------------------------------------------
# classify_dial_error -- the same rule for every provider
# ---------------------------------------------------------------------------
def rest_exception(status: int) -> TwilioRestException:
    return TwilioRestException(status=status, uri="/Calls", msg="denied")


def test_twilio_4xx_is_a_proven_rejection():
    provider, _ = twilio_provider()
    for status in (400, 401, 404, 429):
        assert provider.classify_dial_error(rest_exception(status)) == "rejected"


def test_twilio_5xx_and_transport_failures_are_ambiguous():
    """A 5xx or a timeout may have placed a real call. Marking it rejected
    would release the phone and let the queue dial the customer again."""
    provider, _ = twilio_provider()
    assert provider.classify_dial_error(rest_exception(500)) == "ambiguous"
    assert provider.classify_dial_error(rest_exception(503)) == "ambiguous"
    assert provider.classify_dial_error(TimeoutError("read timed out")) == "ambiguous"
    assert provider.classify_dial_error(ConnectionResetError()) == "ambiguous"


def test_an_unrecognised_exception_is_ambiguous_not_rejected():
    """The default must fail toward never redialing."""
    provider, _ = twilio_provider()

    class Weird(Exception):
        status = "not-an-int"

    assert provider.classify_dial_error(Weird()) == "ambiguous"
    assert provider.classify_dial_error(RuntimeError("?")) == "ambiguous"


# ---------------------------------------------------------------------------
# Terminal request mapping
# ---------------------------------------------------------------------------
def test_twilio_cancels_before_answer_and_completes_after():
    provider, _ = twilio_provider()
    assert provider.terminal_request_for("ringing") == "canceled"
    assert provider.terminal_request_for("queued") == "canceled"
    assert provider.terminal_request_for("initiated") == "canceled"
    assert provider.terminal_request_for("in-progress") == "completed"


def test_terminal_request_helper_is_shared_and_explicit():
    assert terminal_request_for("ringing", frozenset({"ringing"})) == "canceled"
    assert terminal_request_for("ringing", frozenset()) == "completed"


# ---------------------------------------------------------------------------
# Configuration reporting
# ---------------------------------------------------------------------------
def test_twilio_reports_missing_settings_by_name_never_by_value():
    provider = TwilioProvider(client=FakeClient(), from_number="", public_base_url="")
    configured, missing = provider.is_configured()

    assert configured is False
    assert "TWILIO_FROM_NUMBER" in missing and "PUBLIC_BASE_URL" in missing


def test_registry_knows_both_providers_and_rejects_anything_else():
    assert set(PROVIDER_NAMES) == {"twilio", "exotel"}
    assert DEFAULT_PROVIDER == "twilio"
    assert get_provider("twilio").name == "twilio"
    with pytest.raises(ValueError):
        get_provider("carrier-pigeon")


def test_dial_result_defaults_to_no_status():
    assert DialResult("CA1").provider_status is None
