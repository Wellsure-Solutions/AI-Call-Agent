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
from app.telephony.providers.teler_provider import TelerDialError, TelerProvider
from app.telephony.providers.twilio_provider import TwilioProvider

API_KEY = "exotel-key-not-real"
API_TOKEN = "exotel-token-not-real"
TELER_KEY = "teler-key-not-real"


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
# Teler descriptions
# ---------------------------------------------------------------------------
def teler(handler) -> TelerProvider:
    return TelerProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        api_key=TELER_KEY, from_number="+918064000000",
        base_url="https://api.frejun.ai/api/v1", record=False,
    )


def teler_error(response: httpx.Response) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError("boom", request=httpx.Request("POST", "https://x"), response=response)


def test_a_teler_description_carries_the_message_and_the_machine_readable_code():
    """FreJun's own docs say to branch on `code`, not on `message`, so an
    operator needs both to find the failure in their console."""
    provider = teler(lambda _r: httpx.Response(202))
    error = teler_error(httpx.Response(
        403, json={"success": False, "message": "Virtual number is not assigned to a voice app.",
                   "code": "number_unassigned"}))

    detail = provider.describe_dial_error(error)

    assert "HTTP 403" in detail
    assert "not assigned" in detail
    assert "number_unassigned" in detail


def test_two_different_teler_failures_produce_different_descriptions():
    """The whole point: `HTTPStatusError` on every row is undiagnosable."""
    provider = teler(lambda _r: httpx.Response(202))
    unassigned = provider.describe_dial_error(teler_error(httpx.Response(
        403, json={"message": "Virtual number is not assigned to a voice app."})))
    invalid = provider.describe_dial_error(teler_error(httpx.Response(
        422, json={"message": "to_number is not a valid E.164 number."})))

    assert unassigned != invalid


def test_a_teler_description_never_leaks_the_api_key_even_if_echoed():
    provider = teler(lambda _r: httpx.Response(202))
    error = teler_error(httpx.Response(400, json={"message": f"bad key {TELER_KEY}"}))

    detail = provider.describe_dial_error(error)

    assert TELER_KEY not in detail
    assert "<redacted>" in detail


def test_a_teler_description_never_leaks_a_live_flow_or_media_token():
    """The dial body carries our flow_url and status_callback_url, and both
    carry an HMAC. A carrier echoing the request back must not put one in the
    database and the operations view."""
    provider = teler(lambda _r: httpx.Response(202))
    echoed = ("could not reach flow_url "
              "https://x/teler/flow/c1?expiry=99&token=deadbeefdeadbeefdeadbeefdeadbeef")
    error = teler_error(httpx.Response(400, json={"message": echoed}))

    detail = provider.describe_dial_error(error)

    assert "deadbeef" not in detail
    assert "token=<redacted>" in detail


def test_a_teler_body_that_is_not_an_envelope_is_still_described():
    """A proxy in front of Teler will not answer in Teler's envelope, and that
    body is still the only thing that says what happened."""
    provider = teler(lambda _r: httpx.Response(202))
    error = teler_error(httpx.Response(502, text="<html>Bad Gateway</html>"))

    assert "Bad Gateway" in provider.describe_dial_error(error)


def test_a_teler_accepted_but_unnameable_call_describes_the_body():
    provider = teler(lambda _r: httpx.Response(202))
    error = TelerDialError("response contained no call identifier", body='{"message": "queued"}')

    detail = provider.describe_dial_error(error)

    assert "no call identifier" in detail and "queued" in detail


def test_a_teler_transport_failure_is_described_without_a_response():
    provider = teler(lambda _r: httpx.Response(202))
    assert "ConnectTimeout" in provider.describe_dial_error(httpx.ConnectTimeout("timed out"))


# ---------------------------------------------------------------------------
# Teler classification -- the direction that risks calling somebody twice
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [400, 403, 404, 409, 422, 429])
def test_a_teler_4xx_is_a_proven_refusal(status):
    provider = teler(lambda _r: httpx.Response(202))
    error = teler_error(httpx.Response(status, json={"message": "no"}))

    assert provider.classify_dial_error(error) == "rejected"


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_teler_5xx_is_ambiguous_because_the_call_may_have_been_placed(status):
    """Teler documents 502 and 504 on initiate as upstream failures. An
    upstream carrier that timed out may still have dialled, so filing it as a
    refusal would free the number and call the customer twice."""
    provider = teler(lambda _r: httpx.Response(202))
    error = teler_error(httpx.Response(status, json={"message": "upstream"}))

    assert provider.classify_dial_error(error) == "ambiguous"


@pytest.mark.parametrize("error", [
    httpx.ConnectTimeout("timed out"),
    httpx.ReadTimeout("read timed out"),
    httpx.ConnectError("refused"),
    TelerDialError("no call identifier", body="{}"),
    TimeoutError("something else entirely"),
    RuntimeError("unrecognised"),
])
def test_everything_that_is_not_a_proven_refusal_is_ambiguous(error):
    provider = teler(lambda _r: httpx.Response(202))
    assert provider.classify_dial_error(error) == "ambiguous"


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


# ---------------------------------------------------------------------------
# End to end: the *reconciliation* failure is as diagnosable as the dial one
#
# This path stored `type(error).__name__` and nothing else, so it had exactly
# the defect the dial path was already fixed for -- and it matters more here,
# not less. A call stalls in NEEDS_RECONCILIATION holding a capacity slot,
# and at the default ceiling of one that is a stalled queue; `HTTPStatusError`
# does not tell an operator whether to rotate a key, fix a tunnel, or wait.
# ---------------------------------------------------------------------------
def run_reconcile(store: SQLiteCallStore, handler, provider_name: str = "exotel", factory=None,
                  phone: str = "+919000030001") -> str:
    """Bind a call, force the ring deadline past, then reconcile it."""
    call_id = store.enqueue_call(phone_number=phone, provider=provider_name)["call_id"]
    store.claim_job("dialer", 1)
    store.bind_call_sid(call_id, f"SID{phone[-4:]}", 45, 900)
    with store.transaction(immediate=True) as db:
        db.execute("UPDATE calls SET ring_deadline='1970-01-01T00:00:00+00:00' WHERE call_id=?", (call_id,))

    async def scenario():
        coordinator = DurableCallCoordinator(
            store, 1, 0, provider_factory=factory or (lambda name: exotel(handler))
        )
        action = store.claim_due_action(coordinator.owner)
        await coordinator._reconcile(action)

    asyncio.run(scenario())
    return call_id


def test_a_failed_reconciliation_stores_the_carrier_message_not_just_the_class_name(store):
    call_id = run_reconcile(store, lambda r: httpx.Response(503, text="upstream gateway unavailable"))

    saved = store.get_call(call_id)

    assert "upstream gateway unavailable" in saved["reconciliation_error"]
    assert saved["reconciliation_error"] != "HTTPStatusError"


def test_two_different_reconciliation_failures_are_distinguishable(store):
    """The whole point. Both used to write `HTTPStatusError`."""
    gone = run_reconcile(store, lambda r: httpx.Response(404, text="No call found for that Sid"),
                         phone="+919000030002")
    denied = run_reconcile(store, lambda r: httpx.Response(401, text="Authentication failed"),
                           phone="+919000030003")

    assert "No call found" in store.get_call(gone)["reconciliation_error"]
    assert "Authentication failed" in store.get_call(denied)["reconciliation_error"]


def test_the_stored_reconciliation_detail_is_credential_free(store):
    """Same non-negotiable as the dial path: the operations view and the
    database never see a live credential."""
    call_id = run_reconcile(store, lambda r: httpx.Response(401, text=f"bad auth {API_TOKEN}"))

    saved = store.get_call(call_id)

    assert API_TOKEN not in saved["reconciliation_error"]
    assert API_KEY not in saved["reconciliation_error"]


def test_a_teler_reconciliation_failure_carries_telers_own_message(store):
    """Every provider, not just the one this was written against."""
    def handler(_request):
        return httpx.Response(404, json={"message": "The requested call was not found.",
                                         "code": "call_not_found"})

    call_id = run_reconcile(store, handler, provider_name="teler",
                            factory=lambda name: teler(handler))

    saved = store.get_call(call_id)

    assert "HTTP 404" in saved["reconciliation_error"]
    assert "was not found" in saved["reconciliation_error"]
    assert "call_not_found" in saved["reconciliation_error"]


def test_a_provider_the_registry_does_not_know_still_records_the_failure(store):
    """`_provider_for` raises before `provider` is bound -- a call row written
    by a build that had a carrier this one does not. Referencing an unbound
    local inside the handler would raise, lose the reconciliation result
    entirely, and leave the action lease held until it expired."""
    def explode(_name):
        raise ValueError("unknown telephony provider: 'carrier-pigeon'")

    call_id = run_reconcile(store, None, provider_name="exotel", factory=explode)

    saved = store.get_call(call_id)

    assert saved["reconciliation_error"], "the failure must still be recorded"
    assert saved["reconciliation_attempts"] == 1
    assert saved["reconciliation_status"] in {"retry_pending", "stalled"}


def test_a_provider_whose_description_raises_does_not_lose_the_reconciliation(store):
    """Losing the detail is acceptable; losing the record of the failure is
    not -- this runs inside the failure handler."""
    class Hostile:
        name = "exotel"

        async def fetch_status(self, _sid):
            raise RuntimeError("lookup exploded")

        def describe_dial_error(self, error):
            raise ValueError("description exploded")

    call_id = run_reconcile(store, None, factory=lambda name: Hostile())

    saved = store.get_call(call_id)

    assert saved["reconciliation_error"] == "RuntimeError", "degraded to the class name, not lost"
    assert saved["reconciliation_attempts"] == 1
