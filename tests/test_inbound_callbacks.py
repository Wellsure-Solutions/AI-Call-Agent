from __future__ import annotations

"""Inbound calls: somebody ringing the virtual number back.

Three things are being protected here.

**The endpoint is a public write.** FreJun's Incoming Call URL is configured
once on the Voice App, so it cannot carry the per-call HMAC the outbound flow
uses -- there is no call to mint one against when the URL is typed in. A single
shared secret is the available control, and it has to fail closed: an
unauthenticated endpoint lets anyone who guesses the path fill the operator's
follow-up list with numbers that never rang.

**A retry must not duplicate a callback.** Teler re-POSTs a flow request whose
response it did not receive cleanly. One person ringing once must be one row.

**The migration must not touch existing data.** This ships to a database with
real leads and call history in it.
"""


import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.storage.sqlite_store import SQLiteCallStore
from app.telephony import teler_routes

KEY = "incoming-secret-not-real"


@pytest.fixture(autouse=True)
def incoming_secret(monkeypatch):
    monkeypatch.setattr(teler_routes, "TELER_INCOMING_SECRET", KEY)
    monkeypatch.setattr(teler_routes, "TELER_INCOMING_MEDIA_URL", "")
    yield


@pytest.fixture()
def store(tmp_path) -> SQLiteCallStore:
    built = SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)
    teler_routes.configure(built, None)
    return built


@pytest.fixture()
def client(store) -> TestClient:
    app = FastAPI()
    app.include_router(teler_routes.router)
    return TestClient(app)


def incoming_body(call_id: str = "cs_in_1", from_number: str = "+919352596681") -> dict:
    """What Teler POSTs to the Incoming Call URL.

    Same schema as the outbound flow request -- FreJun document one shape for
    both directions, distinguished only by `direction`.
    """
    return {
        "call_id": call_id,
        "account_id": "41dac8c0-56c1-4747-8675-0eda80b03c34",
        "from_number": from_number,
        "to_number": "+918065177514",
        "direction": "inbound",
    }


def post_incoming(client: TestClient, body: dict | None = None, key: str | None = KEY):
    params = {} if key is None else {"key": key}
    return client.post("/teler/incoming", params=params, json=body or incoming_body())


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def test_a_correctly_keyed_incoming_call_is_accepted(client, store):
    response = post_incoming(client)

    assert response.status_code == 200
    assert len(store.list_inbound_calls()) == 1


def test_an_incoming_call_with_no_key_is_refused_and_records_nothing(client, store):
    response = post_incoming(client, key=None)

    assert response.status_code == 403
    assert store.list_inbound_calls() == []


def test_an_incoming_call_with_the_wrong_key_is_refused(client, store):
    response = post_incoming(client, key="not-the-secret")

    assert response.status_code == 403
    assert store.list_inbound_calls() == []


def test_an_unset_secret_refuses_everything_rather_than_allowing_everything(client, store, monkeypatch):
    """Fail closed. The alternative -- treating an unset secret as "no auth
    required" -- turns a missing environment variable into a public write."""
    monkeypatch.setattr(teler_routes, "TELER_INCOMING_SECRET", "")

    assert post_incoming(client, key="").status_code == 403
    assert post_incoming(client, key=None).status_code == 403
    assert store.list_inbound_calls() == []


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
def test_the_callback_records_the_caller_and_starts_unhandled(client, store):
    post_incoming(client)

    row = store.list_inbound_calls()[0]
    assert row["from_number"] == "+919352596681"
    assert row["to_number"] == "+918065177514"
    assert row["status"] == "received"
    assert row["handled"] == 0


def test_a_retried_flow_request_does_not_duplicate_the_callback(client, store):
    """Teler retries a flow request it did not get a clean response to. One
    person ringing once must be one row in the operator's follow-up list."""
    for _ in range(3):
        post_incoming(client)

    assert len(store.list_inbound_calls()) == 1


def test_two_different_callers_are_two_callbacks(client, store):
    post_incoming(client, incoming_body("cs_in_1", "+919352596681"))
    post_incoming(client, incoming_body("cs_in_2", "+919812345678"))

    assert len(store.list_inbound_calls()) == 2


def test_a_known_number_is_matched_to_its_lead(client, store):
    lead = store.import_leads([
        {"business_name": "Sharma Electronics", "phone_number": "+919352596681",
         "category": "Retail", "city": "Jaipur", "notes": ""}
    ])["leads"][0]

    post_incoming(client)

    row = store.list_inbound_calls()[0]
    assert row["lead_id"] == lead["lead_id"]
    assert row["business_name"] == "Sharma Electronics"
    assert row["city"] == "Jaipur"


def test_a_caller_id_in_local_format_still_matches_the_lead(client, store):
    """Carriers do not agree on E.164. A lead stored as +91... must still be
    found when the caller ID arrives as ten bare digits, or every callback from
    a known seller shows up as an unknown number."""
    store.import_leads([
        {"business_name": "Acme", "phone_number": "+919352596681", "category": "Retail", "notes": ""}
    ])

    post_incoming(client, incoming_body("cs_local", "9352596681"))

    row = store.list_inbound_calls()[0]
    assert row["from_number"] == "+919352596681"
    assert row["business_name"] == "Acme"


def test_an_unknown_number_is_still_recorded(client, store):
    """A callback from somebody not in the lead list is still a callback."""
    post_incoming(client, incoming_body("cs_x", "+919999900000"))

    row = store.list_inbound_calls()[0]
    assert row["lead_id"] is None
    assert row["business_name"] == ""


def test_an_unparseable_caller_id_is_recorded_rather_than_dropped(client, store):
    """`anonymous` will not normalise. Losing the row loses the only signal
    this table exists for -- that the phone rang."""
    post_incoming(client, incoming_body("cs_anon", "anonymous"))

    assert len(store.list_inbound_calls()) == 1


def test_a_storage_failure_still_answers_teler(client, store, monkeypatch):
    """A 5xx makes Teler retry, which either duplicates the callback or leaves
    the caller on silence while we retry a database write."""
    async def explode(**_kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(store, "arecord_inbound_call", explode)

    assert post_incoming(client).status_code == 200


# ---------------------------------------------------------------------------
# The flow returned to the caller
# ---------------------------------------------------------------------------
def test_the_default_flow_hangs_up(client):
    """One action per flow, so `play` and `hangup` are mutually exclusive.
    Hangup is the default because it needs no hosted audio."""
    assert post_incoming(client).json() == {"action": "hangup"}


def test_a_configured_media_url_is_played_instead(client, monkeypatch):
    monkeypatch.setattr(teler_routes, "TELER_INCOMING_MEDIA_URL", "https://cdn.example.invalid/thanks.mp3")

    assert post_incoming(client).json() == {
        "action": "play",
        "media_url": "https://cdn.example.invalid/thanks.mp3",
    }


def test_the_agent_is_never_run_on_an_inbound_call(client):
    """Answering an inbound caller with the outbound pitch is the wrong
    conversation, and it would also bill for the whole exchange."""
    flow = post_incoming(client).json()

    assert flow["action"] != "stream"
    assert "ws_url" not in flow


# ---------------------------------------------------------------------------
# The inbound status webhook
# ---------------------------------------------------------------------------
def status_body(call_id: str, event: str = "call.completed", duration: int = 42) -> dict:
    return {
        "event": event,
        "account_id": "41dac8c0-56c1-4747-8675-0eda80b03c34",
        "data": {"call_id": call_id, "duration": duration, "hangup_source": "caller"},
    }


def test_the_status_webhook_updates_the_callback(client, store):
    post_incoming(client)

    response = client.post("/teler/incoming/status", params={"key": KEY}, json=status_body("cs_in_1"))

    assert response.status_code == 200
    row = store.list_inbound_calls()[0]
    assert row["status"] == "completed"
    assert row["duration"] == 42


def test_the_status_webhook_needs_the_key_too(client, store):
    post_incoming(client)

    assert client.post("/teler/incoming/status", json=status_body("cs_in_1")).status_code == 403
    assert store.list_inbound_calls()[0]["status"] == "received"


def test_a_status_for_an_unknown_call_creates_nothing(client, store):
    """Outbound calls report to their own per-call URL. Anything arriving here
    we cannot match is acknowledged and dropped, never invented -- a caller in
    the dashboard with no record of why they are there is worse than a gap."""
    response = client.post("/teler/incoming/status", params={"key": KEY}, json=status_body("cs_never_seen"))

    assert response.status_code == 200
    assert store.list_inbound_calls() == []


def test_a_stream_event_does_not_overwrite_the_call_status(client, store):
    post_incoming(client)
    client.post("/teler/incoming/status", params={"key": KEY}, json=status_body("cs_in_1", "call.completed"))

    client.post("/teler/incoming/status", params={"key": KEY},
                json=status_body("cs_in_1", "stream.completed"))

    assert store.list_inbound_calls()[0]["status"] == "completed"


# ---------------------------------------------------------------------------
# Store-level management
# ---------------------------------------------------------------------------
def test_marking_handled_and_reopening_round_trips(store):
    row = store.record_inbound_call(from_number="+919352596681", provider_call_id="cs_1")

    done = store.set_inbound_handled(row["inbound_id"], True, "Called back, not interested")
    assert done["handled"] == 1 and done["handled_at"] and done["notes"] == "Called back, not interested"

    reopened = store.set_inbound_handled(row["inbound_id"], False)
    assert reopened["handled"] == 0 and reopened["handled_at"] is None


def test_the_handled_filter_selects_the_follow_up_queue(store):
    first = store.record_inbound_call(from_number="+919352596681", provider_call_id="a")
    store.record_inbound_call(from_number="+919812345678", provider_call_id="b")
    store.set_inbound_handled(first["inbound_id"], True)

    assert len(store.list_inbound_calls(handled="false")) == 1
    assert len(store.list_inbound_calls(handled="true")) == 1
    assert len(store.list_inbound_calls()) == 2


def test_statistics_counts_callbacks_and_the_pending_ones(store):
    first = store.record_inbound_call(from_number="+919352596681", provider_call_id="a")
    store.record_inbound_call(from_number="+919812345678", provider_call_id="b")
    store.set_inbound_handled(first["inbound_id"], True)

    stats = store.statistics()
    assert stats["inbound_calls"] == 2
    assert stats["inbound_pending"] == 1


def test_deleting_a_callback_leaves_the_rest(store):
    first = store.record_inbound_call(from_number="+919352596681", provider_call_id="a")
    store.record_inbound_call(from_number="+919812345678", provider_call_id="b")

    assert store.delete_inbound_call(first["inbound_id"]) is True
    assert store.delete_inbound_call(first["inbound_id"]) is False
    assert len(store.list_inbound_calls()) == 1


def test_clearing_all_leads_does_not_fail_on_a_workspace_with_callbacks(store):
    """The same foreign key, on the bulk path. Without detaching callbacks
    first, "clear leads" raises and the operator cannot clear anything."""
    store.import_leads([
        {"business_name": "Acme", "phone_number": "+919352596681", "category": "Retail", "notes": ""}
    ])
    store.record_inbound_call(from_number="+919352596681", provider_call_id="a")

    deleted = store.clear_data("leads")

    assert deleted["leads"] == 1
    assert len(store.list_inbound_calls()) == 1
    assert store.list_inbound_calls()[0]["lead_id"] is None


def test_a_deleted_lead_does_not_erase_who_rang(store):
    """Business details are copied onto the callback rather than joined at read
    time, so call history does not silently rewrite itself when a lead goes."""
    lead = store.import_leads([
        {"business_name": "Sharma Electronics", "phone_number": "+919352596681",
         "category": "Retail", "notes": ""}
    ])["leads"][0]
    store.record_inbound_call(from_number="+919352596681", provider_call_id="a")

    store.delete_lead(lead["lead_id"])

    assert store.list_inbound_calls()[0]["business_name"] == "Sharma Electronics"


# ---------------------------------------------------------------------------
# Manual do-not-call, which extraction used to do automatically
# ---------------------------------------------------------------------------
def test_suppressing_a_number_blocks_future_calls_and_flags_the_lead(store):
    from app.storage.sqlite_store import SuppressedError

    lead = store.import_leads([
        {"business_name": "Acme", "phone_number": "+919352596681", "category": "Retail", "notes": ""}
    ])["leads"][0]

    assert store.suppress_phone("9352596681") is True

    assert store.get_lead(lead["lead_id"])["do_not_call"] == 1
    with pytest.raises(SuppressedError):
        store.enqueue_call(phone_number="+919352596681", lead_id=lead["lead_id"])


def test_suppressing_a_nonsense_number_reports_failure_rather_than_writing(store):
    assert store.suppress_phone("not a phone") is False


# ---------------------------------------------------------------------------
# The migration must not disturb an existing production database
# ---------------------------------------------------------------------------
def test_reopening_a_pre_v5_database_adds_the_table_and_keeps_every_row(tmp_path):
    """The requirement this ships under: migrate, never delete.

    Simulates the production database as it stands -- leads, call history and
    saved settings, at schema v4 with no inbound table -- and reopens it.
    """
    path = tmp_path / "prod.sqlite3"
    original = SQLiteCallStore(path, tmp_path)
    lead = original.import_leads([
        {"business_name": "Sharma Electronics", "phone_number": "+919352596681",
         "category": "Retail", "city": "Jaipur", "notes": "existing"}
    ])["leads"][0]
    call = original.enqueue_call(phone_number="+919352596681", lead_id=lead["lead_id"])
    original.set_active_provider("teler")

    # Wind the database back to v4, exactly as it would be before this deploy.
    with original.transaction(immediate=True) as db:
        db.execute("DROP TABLE IF EXISTS inbound_calls")
        db.execute("UPDATE schema_metadata SET value='4' WHERE key='schema_version'")

    reopened = SQLiteCallStore(path, tmp_path)

    with reopened.transaction() as db:
        version = db.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0]
    assert version == "5"
    assert reopened.get_lead(lead["lead_id"])["business_name"] == "Sharma Electronics"
    assert reopened.get_lead(lead["lead_id"])["notes"] == "existing"
    assert reopened.get_call(call["call_id"])["phone_number"] == "+919352596681"
    assert reopened.active_provider_setting() == "teler"
    assert reopened.list_inbound_calls() == []
    # And the new table actually works on the migrated database.
    reopened.record_inbound_call(from_number="+919352596681", provider_call_id="cs_after")
    assert len(reopened.list_inbound_calls()) == 1


def test_the_migration_is_idempotent_and_keeps_callbacks_across_restarts(tmp_path):
    path = tmp_path / "repeat.sqlite3"
    first = SQLiteCallStore(path, tmp_path)
    first.record_inbound_call(from_number="+919352596681", provider_call_id="cs_keep")

    for _ in range(3):
        SQLiteCallStore(path, tmp_path)

    assert len(SQLiteCallStore(path, tmp_path).list_inbound_calls()) == 1


def test_no_migration_step_drops_or_rewrites_anything(tmp_path):
    """A structural guard on the whole chain, not just v5.

    Every migration in this store is additive by design -- CREATE TABLE IF NOT
    EXISTS and guarded ALTER ADD COLUMN. A DROP TABLE or DELETE appearing in
    one would take production data with it on the next deploy, and would be
    found in production rather than here.
    """
    import inspect

    from app.storage import sqlite_store

    source = "\n".join(
        inspect.getsource(getattr(SQLiteCallStore, name))
        for name in dir(SQLiteCallStore)
        if name.startswith("_migrate_schema_v")
    )
    lowered = source.lower()
    assert "drop table" not in lowered
    assert "delete from" not in lowered
    assert "drop column" not in lowered
    assert sqlite_store is not None


# ---------------------------------------------------------------------------
# The operator API the Callbacks tab is built on
# ---------------------------------------------------------------------------
@pytest.fixture()
def api(store) -> TestClient:
    """The real handlers, rebound to a temp store.

    Importing app.main would open the production database path, so the
    endpoints are mounted here instead; the logic under test is theirs.
    """
    import app.main as main

    app = FastAPI()
    app.add_api_route("/api/callbacks", main.list_callbacks, methods=["GET"])
    app.add_api_route("/api/callbacks/{inbound_id}/handled", main.set_callback_handled, methods=["POST"])
    app.add_api_route("/api/callbacks/{inbound_id}", main.delete_callback, methods=["DELETE"])
    app.add_api_route("/api/suppress", main.suppress_number, methods=["POST"])
    main.answer_store = store
    return TestClient(app)


def test_the_api_lists_callbacks_newest_first(api, store):
    store.record_inbound_call(from_number="+919352596681", provider_call_id="a")
    store.record_inbound_call(from_number="+919812345678", provider_call_id="b")

    body = api.get("/api/callbacks").json()

    assert len(body["callbacks"]) == 2
    assert body["callbacks"][0]["received_at"] >= body["callbacks"][1]["received_at"]


def test_the_api_filters_to_the_follow_up_queue(api, store):
    first = store.record_inbound_call(from_number="+919352596681", provider_call_id="a")
    store.record_inbound_call(from_number="+919812345678", provider_call_id="b")
    store.set_inbound_handled(first["inbound_id"], True)

    pending = api.get("/api/callbacks", params={"handled": "false"}).json()["callbacks"]

    assert len(pending) == 1
    assert pending[0]["from_number"] == "+919812345678"


def test_the_api_marks_a_callback_handled(api, store):
    row = store.record_inbound_call(from_number="+919352596681", provider_call_id="a")

    response = api.post(f"/api/callbacks/{row['inbound_id']}/handled", json={"handled": True})

    assert response.status_code == 200
    assert response.json()["handled"] == 1


def test_the_api_404s_on_an_unknown_callback(api):
    assert api.post("/api/callbacks/nope/handled", json={"handled": True}).status_code == 404
    assert api.delete("/api/callbacks/nope").status_code == 404


def test_the_api_suppresses_a_number(api, store):
    store.import_leads([
        {"business_name": "Acme", "phone_number": "+919352596681", "category": "Retail", "notes": ""}
    ])

    response = api.post("/api/suppress", json={"phone_number": "+919352596681"})

    assert response.status_code == 200
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM suppression_list").fetchone()[0] == 1


def test_the_api_rejects_a_suppression_with_no_number(api):
    assert api.post("/api/suppress", json={}).status_code == 422
    assert api.post("/api/suppress", json={"phone_number": "nonsense"}).status_code == 422
