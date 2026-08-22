from __future__ import annotations

"""The coordinator dialling through the real ExotelProvider.

Exotel binds the CallSid in the dial *response*, not on a later webhook, so
the whole correlation chain shifts one step earlier than Twilio's. These tests
drive the actual provider over a mock transport to check that the durable
deadlines are still set from the dial path, and that the three failure modes
land where the durable design requires.
"""

import asyncio

import httpx
import pytest

from app.services.call_coordinator import DurableCallCoordinator
from app.storage.sqlite_store import SQLiteCallStore
from app.telephony.providers.exotel_provider import ExotelProvider


@pytest.fixture()
def store(tmp_path) -> SQLiteCallStore:
    return SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setattr("app.telephony.callback_urls.STREAM_SECRET", "test-secret-not-real")
    monkeypatch.setattr("app.telephony.callback_urls.PUBLIC_BASE_URL", "https://calls.example.invalid")


def exotel_factory(handler, seen: list[httpx.Request]):
    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(name: str):
        assert name == "exotel", f"expected the call's persisted provider, got {name!r}"
        return ExotelProvider(
            client=httpx.AsyncClient(transport=httpx.MockTransport(capture)),
            account_sid="acme", api_key="k", api_token="t",
            subdomain="api.in.exotel.com", caller_id="+918047000000",
        )

    return factory


def queue_exotel_call(store: SQLiteCallStore, phone: str) -> str:
    return store.enqueue_call(phone_number=phone, provider="exotel")["call_id"]


def run_dial(store: SQLiteCallStore, call_id: str, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    async def scenario():
        coordinator = DurableCallCoordinator(
            store, 1, 0, ring_timeout=45, max_call_seconds=900,
            provider_factory=exotel_factory(handler, seen),
        )
        claimed = store.claim_job(coordinator.owner, 1)
        assert claimed["call_id"] == call_id
        await coordinator._dial(claimed)

    asyncio.run(scenario())
    return seen


def ok(sid="exotel-sid-1", status="in-progress"):
    return lambda request: httpx.Response(200, json={"Call": {"Sid": sid, "Status": status}})


# ---------------------------------------------------------------------------
# The happy path: bound at dial time
# ---------------------------------------------------------------------------
def test_the_sid_is_bound_from_the_dial_path_with_both_durable_deadlines(store):
    """Twilio binds in the TwiML webhook; Exotel has no such step. The
    deadlines must still be set, from here."""
    call_id = queue_exotel_call(store, "+919000010001")

    run_dial(store, call_id, ok())

    saved = store.get_call(call_id)
    assert saved["call_sid"] == "exotel-sid-1"
    assert saved["ring_deadline"] is not None, "ring deadline must be set at dial"
    assert saved["max_call_deadline"] is not None, "max-call deadline must be set at dial"
    assert saved["lifecycle_state"] != "NEEDS_RECONCILIATION"
    assert store._one("SELECT queue_state FROM call_jobs WHERE call_id=?", (call_id,))["queue_state"] == "active"


def test_the_dial_carries_a_signed_stream_url_and_status_callback(store):
    """Exotel is told where to stream at dial time, so the token has to be in
    the URL by then -- there is no later webhook to mint one."""
    from urllib.parse import parse_qs, parse_qsl, urlparse

    call_id = queue_exotel_call(store, "+919000010002")

    seen = run_dial(store, call_id, ok())

    fields = dict(parse_qsl(seen[0].content.decode()))
    stream = urlparse(fields["StreamUrl"])
    assert stream.scheme == "wss" and stream.path == "/exotel/media-stream"
    query = parse_qs(stream.query)
    assert query["call_id"] == [call_id]

    from app.telephony.callback_urls import valid_exotel_callback_token, valid_exotel_stream_token

    assert valid_exotel_stream_token(call_id, int(query["expiry"][0]), query["token"][0])

    callback = urlparse(fields["StatusCallback"])
    callback_query = parse_qs(callback.query)
    assert callback.path == f"/exotel/status/{call_id}"
    assert valid_exotel_callback_token(call_id, int(callback_query["expiry"][0]), callback_query["token"][0])


def test_the_destination_is_the_dialled_number_not_the_exophone(store):
    from urllib.parse import parse_qsl

    call_id = queue_exotel_call(store, "+919000010003")

    seen = run_dial(store, call_id, ok())

    fields = dict(parse_qsl(seen[0].content.decode()))
    assert fields["From"] == "+919000010003"
    assert fields["CallerId"] == "+918047000000"


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------
def test_a_4xx_is_terminal_and_releases_the_slot(store):
    call_id = queue_exotel_call(store, "+919000010004")

    run_dial(store, call_id, lambda request: httpx.Response(400, json={"message": "invalid number"}))

    saved = store.get_call(call_id)
    assert saved["lifecycle_state"] == "FAILED"
    assert saved["outcome"] == "provider_rejected"
    assert store.capacity_snapshot()["capacity_occupied"] == 0


def test_a_5xx_is_ambiguous_and_is_never_redialed(store):
    call_id = queue_exotel_call(store, "+919000010005")

    run_dial(store, call_id, lambda request: httpx.Response(503, text="upstream down"))

    saved = store.get_call(call_id)
    assert saved["lifecycle_state"] == "NEEDS_RECONCILIATION"
    assert saved["provider_terminal_at"] is None, "no outcome may be invented"
    # The phone stays blocked: re-enqueuing returns the same unresolved call.
    assert store.enqueue_call(phone_number="+919000010005")["call_id"] == call_id


def test_a_200_with_an_error_payload_is_ambiguous_not_rejected(store):
    """The guard for carrier APIs that answer 200 with a failure body. The
    call may really have been placed, so it must not free the phone."""
    call_id = queue_exotel_call(store, "+919000010006")

    run_dial(store, call_id, lambda request: httpx.Response(200, json={"status": "failure"}))

    saved = store.get_call(call_id)
    assert saved["lifecycle_state"] == "NEEDS_RECONCILIATION"
    assert saved["outcome"] != "provider_rejected"
    assert saved["provider_terminal_at"] is None


def test_a_dial_that_returns_no_sid_takes_the_existing_reconciliation_path(store):
    """Unchanged from Twilio: accepted but unidentifiable is ambiguous."""
    call_id = queue_exotel_call(store, "+919000010007")

    run_dial(store, call_id, lambda request: httpx.Response(200, json={"Call": {"Status": "queued"}}))

    saved = store.get_call(call_id)
    assert saved["call_sid"] is None
    assert saved["lifecycle_state"] == "NEEDS_RECONCILIATION"
    assert saved["provider_terminal_at"] is None


def test_a_timeout_is_ambiguous(store):
    call_id = queue_exotel_call(store, "+919000010008")

    def explode(request):
        raise httpx.ConnectTimeout("no route", request=request)

    run_dial(store, call_id, explode)

    assert store.get_call(call_id)["lifecycle_state"] == "NEEDS_RECONCILIATION"


# ---------------------------------------------------------------------------
# Reconciliation uses the persisted provider
# ---------------------------------------------------------------------------
def test_reconciliation_asks_exotel_and_terminalizes_on_a_proven_status(store):
    call_id = queue_exotel_call(store, "+919000010009")
    run_dial(store, call_id, ok())
    with store.transaction(immediate=True) as db:
        db.execute("UPDATE calls SET ring_deadline='1970-01-01T00:00:00+00:00' WHERE call_id=?", (call_id,))

    seen: list[httpx.Request] = []

    async def scenario():
        coordinator = DurableCallCoordinator(
            store, 1, 0, provider_factory=exotel_factory(ok(status="no-answer"), seen)
        )
        action = store.claim_due_action(coordinator.owner)
        await coordinator._reconcile(action)

    asyncio.run(scenario())

    saved = store.get_call(call_id)
    assert saved["provider_status"] == "no-answer"
    assert saved["lifecycle_state"] == "NO_ANSWER"
    assert store.capacity_snapshot()["capacity_occupied"] == 0


def test_reconciliation_asks_exotel_to_hang_up_a_still_running_call(store):
    call_id = queue_exotel_call(store, "+919000010010")
    run_dial(store, call_id, ok())
    with store.transaction(immediate=True) as db:
        db.execute("UPDATE calls SET max_call_deadline='1970-01-01T00:00:00+00:00' WHERE call_id=?", (call_id,))

    seen: list[httpx.Request] = []

    async def scenario():
        coordinator = DurableCallCoordinator(
            store, 1, 0, provider_factory=exotel_factory(ok(status="in-progress"), seen)
        )
        action = store.claim_due_action(coordinator.owner)
        await coordinator._reconcile(action)

    asyncio.run(scenario())

    assert any(request.method == "DELETE" for request in seen), "a live call must be asked to end"
    # Capacity stays held until a terminal status proves the call ended.
    assert store.get_call(call_id)["provider_terminal_at"] is None
    assert store.capacity_snapshot()["capacity_occupied"] == 1
