from __future__ import annotations

"""What a failed dial leaves behind for the operator who has to diagnose it.

Before this, every Exotel failure stored the exception class name and nothing
else: a wrong ExoPhone, an account without AgentStream enabled, a malformed
StreamUrl and an upstream outage all wrote `HTTPStatusError` and were
indistinguishable. The carrier's own message is the only thing that separates
them -- and it must reach the database without dragging credentials with it.
"""

import asyncio

import httpx
import pytest
from twilio.base.exceptions import TwilioRestException

from app.services.call_coordinator import DurableCallCoordinator
from app.storage.sqlite_store import SQLiteCallStore
from app.telephony.providers.base import MAX_ERROR_DETAIL, describe_error, scrub
from app.telephony.providers.exotel_provider import ExotelDialError, ExotelProvider
from app.telephony.providers.twilio_provider import TwilioProvider

API_KEY = "exotel-key-not-real"
API_TOKEN = "exotel-token-not-real"


@pytest.fixture()
def store(tmp_path) -> SQLiteCallStore:
    return SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)


def exotel(handler) -> ExotelProvider:
    return ExotelProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        account_sid="acct", api_key=API_KEY, api_token=API_TOKEN,
        subdomain="api.in.exotel.com", caller_id="+918047000000",
    )


def failing_dial(handler) -> tuple[ExotelProvider, Exception]:
    provider = exotel(handler)
    with pytest.raises(Exception) as raised:
        asyncio.run(provider.dial(
            call_id="c", to_number="+919812345678", ring_timeout=45,
            stream_url="wss://x/y", status_callback_url="https://x/z",
        ))
    return provider, raised.value


# ---------------------------------------------------------------------------
# The scrubber
# ---------------------------------------------------------------------------
def test_secrets_are_redacted_by_value():
    text = f"auth failed for key {API_KEY} token {API_TOKEN}"

    cleaned = scrub(text, (API_TOKEN, API_KEY))

    assert API_KEY not in cleaned and API_TOKEN not in cleaned
    assert "<redacted>" in cleaned


def test_url_userinfo_is_redacted():
    """Exotel's own docs show credentials embedded in the URL, so an error
    body echoing a request can carry them."""
    cleaned = scrub("failed calling https://abc123:def456@api.in.exotel.com/v1/Accounts/x/Calls")

    assert "abc123" not in cleaned and "def456" not in cleaned
    assert "//<redacted>@api.in.exotel.com" in cleaned


def test_our_own_media_tokens_are_redacted():
    """A carrier error body that quotes the StreamUrl we sent would otherwise
    put a live media HMAC into the database and the operations view."""
    body = "bad StreamUrl wss://h/exotel/media-stream?call_id=c&expiry=99&token=deadbeefcafe1234"

    cleaned = scrub(body)

    assert "deadbeefcafe1234" not in cleaned
    assert "token=<redacted>" in cleaned
    assert "expiry=<redacted>" in cleaned
    assert "call_id=c" in cleaned, "the call id is not a secret and is useful"


def test_output_is_bounded_and_single_line():
    cleaned = scrub("x" * 5000 + "\n\nmore\ttext")

    assert len(cleaned) <= MAX_ERROR_DETAIL
    assert "\n" not in cleaned and "\t" not in cleaned


def test_a_short_secret_is_not_used_as_a_redaction_pattern():
    """Redacting a 1-2 character 'secret' would blank out ordinary text."""
    assert scrub("the call failed", ("a",)) == "the call failed"


def test_the_default_description_still_carries_the_message():
    assert describe_error(ConnectionResetError("connection reset by peer")) == (
        "ConnectionResetError: connection reset by peer"
    )
    assert describe_error(RuntimeError()) == "RuntimeError"


# ---------------------------------------------------------------------------
# Exotel descriptions
# ---------------------------------------------------------------------------
def test_a_4xx_description_carries_exotels_own_message():
    body = '{"RestException":{"Status":400,"Message":"CallerId is not a valid ExoPhone"}}'
    provider, error = failing_dial(lambda r: httpx.Response(400, text=body))

    detail = provider.describe_dial_error(error)

    assert "HTTP 400" in detail
    assert "CallerId is not a valid ExoPhone" in detail


def test_two_different_failures_produce_different_descriptions():
    """The whole point: they used to be identical."""
    _p1, e1 = failing_dial(lambda r: httpx.Response(400, text="Invalid CallerId"))
    _p2, e2 = failing_dial(lambda r: httpx.Response(403, text="Streaming not enabled on this account"))
    provider = exotel(lambda r: httpx.Response(200))

    assert provider.describe_dial_error(e1) != provider.describe_dial_error(e2)
    assert "Streaming not enabled" in provider.describe_dial_error(e2)


def test_a_200_with_an_error_body_describes_the_body():
    provider, error = failing_dial(
        lambda r: httpx.Response(200, json={"status": "failure", "message": "insufficient balance"})
    )

    detail = provider.describe_dial_error(error)

    assert isinstance(error, ExotelDialError)
    assert "insufficient balance" in detail


def test_a_description_never_leaks_the_api_token_even_if_echoed():
    """Exotel error bodies do echo the request on occasion."""
    provider, error = failing_dial(
        lambda r: httpx.Response(400, text=f"rejected request from {API_KEY}:{API_TOKEN}")
    )

    detail = provider.describe_dial_error(error)

    assert API_TOKEN not in detail and API_KEY not in detail
    assert "<redacted>" in detail


def test_a_description_never_leaks_a_media_token_even_if_echoed():
    provider, error = failing_dial(
        lambda r: httpx.Response(400, text="bad StreamUrl wss://h/x?call_id=c&expiry=1&token=secrethmacvalue")
    )

    detail = provider.describe_dial_error(error)

    assert "secrethmacvalue" not in detail


def test_a_transport_failure_is_described_without_a_response():
    def explode(request):
        raise httpx.ConnectTimeout("timed out connecting", request=request)

    provider, error = failing_dial(explode)

    detail = provider.describe_dial_error(error)

    assert "ConnectTimeout" in detail
    assert "timed out connecting" in detail


def test_descriptions_are_bounded():
    provider, error = failing_dial(lambda r: httpx.Response(500, text="stack trace " * 2000))
    assert len(provider.describe_dial_error(error)) <= MAX_ERROR_DETAIL


# ---------------------------------------------------------------------------
# Twilio descriptions
# ---------------------------------------------------------------------------
def test_twilio_descriptions_carry_the_numeric_error_code():
    provider = TwilioProvider(client=object(), from_number="+15550001111", public_base_url="https://x")
    error = TwilioRestException(status=400, uri="/Calls", msg="The 'To' number is not valid", code=21211)

    detail = provider.describe_dial_error(error)

    assert "HTTP 400" in detail and "21211" in detail and "not valid" in detail


def test_twilio_falls_back_for_a_non_sdk_error():
    provider = TwilioProvider(client=object(), from_number="+1", public_base_url="https://x")
    assert "TimeoutError" in provider.describe_dial_error(TimeoutError("read timeout"))


# ---------------------------------------------------------------------------
# End to end: the detail reaches the call row
# ---------------------------------------------------------------------------
def run_dial(store: SQLiteCallStore, handler) -> str:
    call_id = store.enqueue_call(phone_number="+919000020001", provider="exotel")["call_id"]

    async def scenario():
        coordinator = DurableCallCoordinator(store, 1, 0, provider_factory=lambda name: exotel(handler))
        claimed = store.claim_job(coordinator.owner, 1)
        await coordinator._dial(claimed)

    asyncio.run(scenario())
    return call_id


def test_a_rejected_dial_stores_the_carrier_message_not_just_the_class_name(store):
    call_id = run_dial(store, lambda r: httpx.Response(400, text="CallerId is not a valid ExoPhone"))

    saved = store.get_call(call_id)

    assert saved["lifecycle_state"] == "FAILED"
    assert "CallerId is not a valid ExoPhone" in saved["reconciliation_error"]
    assert saved["reconciliation_error"] != "HTTPStatusError"


def test_an_ambiguous_dial_stores_the_carrier_message_too(store):
    """This path used to write a fixed string into reconciliation_error and the
    exception class name into reconciliation_status, so nothing said why."""
    call_id = run_dial(store, lambda r: httpx.Response(503, text="upstream gateway unavailable"))

    saved = store.get_call(call_id)

    assert saved["lifecycle_state"] == "NEEDS_RECONCILIATION"
    assert "upstream gateway unavailable" in saved["reconciliation_error"]
    # The machine-readable status is still the exception type, for control flow.
    assert saved["reconciliation_status"] == "HTTPStatusError"


def test_the_stored_detail_is_credential_free(store):
    call_id = run_dial(store, lambda r: httpx.Response(400, text=f"bad auth {API_TOKEN}"))

    saved = store.get_call(call_id)

    assert API_TOKEN not in saved["reconciliation_error"]
    events = " ".join(str(row) for row in store.list_events(call_id))
    assert API_TOKEN not in events


def test_the_rejection_event_records_the_detail_for_the_audit_trail(store):
    call_id = run_dial(store, lambda r: httpx.Response(400, text="number is on the DND registry"))

    rejected = [row for row in store.list_events(call_id) if row["event_name"] == "dial_rejected"]

    assert rejected and "DND registry" in rejected[0]["metadata"]


def test_a_provider_whose_description_raises_does_not_lose_the_failure(store):
    """This runs inside the failure handler. Losing the detail is acceptable;
    losing the record of the failure is not."""
    class Hostile:
        name = "exotel"

        async def dial(self, **_kwargs):
            raise RuntimeError("dial exploded")

        def classify_dial_error(self, error):
            return "ambiguous"

        def describe_dial_error(self, error):
            raise ValueError("description exploded")

    call_id = store.enqueue_call(phone_number="+919000020002", provider="exotel")["call_id"]

    async def scenario():
        coordinator = DurableCallCoordinator(store, 1, 0, provider_factory=lambda name: Hostile())
        await coordinator._dial(store.claim_job(coordinator.owner, 1))

    asyncio.run(scenario())

    saved = store.get_call(call_id)
    assert saved["lifecycle_state"] == "NEEDS_RECONCILIATION"
    assert saved["reconciliation_status"] == "RuntimeError"


def test_a_provider_without_a_description_method_still_works(store):
    """describe_dial_error is optional, so fakes and legacy adapters need no
    change."""
    class Minimal:
        name = "exotel"

        async def dial(self, **_kwargs):
            raise RuntimeError("boom")

        def classify_dial_error(self, error):
            return "ambiguous"

    call_id = store.enqueue_call(phone_number="+919000020003", provider="exotel")["call_id"]

    async def scenario():
        coordinator = DurableCallCoordinator(store, 1, 0, provider_factory=lambda name: Minimal())
        await coordinator._dial(store.claim_job(coordinator.owner, 1))

    asyncio.run(scenario())

    assert store.get_call(call_id)["lifecycle_state"] == "NEEDS_RECONCILIATION"
