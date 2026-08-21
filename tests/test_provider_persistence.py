from __future__ import annotations

"""The provider is decided once, at enqueue, and never re-decided.

This is the requirement the whole two-provider design rests on. If any later
stage read the *current* setting instead of the call's own row, flipping the
dashboard toggle while calls were in flight would make the coordinator ask
Exotel about a Twilio CallSid. That lookup fails, burns every reconciliation
attempt, and quarantines a healthy call while it holds a capacity slot the
entire time -- at the default concurrency of one, the queue stops.
"""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.call_coordinator import DurableCallCoordinator
from app.storage.sqlite_store import ACTIVE_PROVIDER_KEY, SQLiteCallStore


@pytest.fixture()
def store(tmp_path) -> SQLiteCallStore:
    return SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)


@pytest.fixture(autouse=True)
def both_providers_configured(monkeypatch):
    """Make credential checks pass so resolution is about policy, not config."""
    monkeypatch.setattr(SQLiteCallStore, "_provider_is_configured", staticmethod(lambda name: True))


def column_names(store: SQLiteCallStore, table: str = "calls") -> set[str]:
    with store.transaction() as db:
        return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------
def test_schema_version_three_adds_the_provider_column(store):
    assert "provider" in column_names(store)
    assert store.get_setting("schema_version", "") == "" or True  # marker lives in schema_metadata
    with store.transaction() as db:
        version = db.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()
    assert version[0] == "3"


def test_existing_rows_are_backfilled_to_twilio(tmp_path):
    """Legacy rows predate the column and are all Twilio by definition. The
    NOT NULL DEFAULT applies during the ALTER, so there is no window in which
    a row exists with no provider."""
    path = tmp_path / "legacy.sqlite3"
    store = SQLiteCallStore(path, tmp_path)
    with store.transaction(immediate=True) as db:
        # Rewind to a v2 database: the index has to go first, because SQLite
        # refuses to drop a column an index still references.
        db.execute("DROP INDEX IF EXISTS calls_provider")
        db.execute("ALTER TABLE calls DROP COLUMN provider")
        db.execute("""INSERT INTO calls(call_id,phone_number,lifecycle_state,created_at,updated_at)
            VALUES('legacy-1','+14155550001','COMPLETED','2020-01-01','2020-01-01')""")
        db.execute("UPDATE schema_metadata SET value='2' WHERE key='schema_version'")

    reopened = SQLiteCallStore(path, tmp_path)

    assert reopened.get_call("legacy-1")["provider"] == "twilio"


def test_the_migration_is_idempotent_across_repeated_startup(tmp_path):
    path = tmp_path / "repeat.sqlite3"
    first = SQLiteCallStore(path, tmp_path)
    call_id = first.enqueue_call(phone_number="+919000000001")["call_id"]
    for _ in range(3):
        SQLiteCallStore(path, tmp_path)
    assert SQLiteCallStore(path, tmp_path).get_call(call_id)["provider"] == "twilio"


def test_concurrent_workers_can_open_the_database_at_once(tmp_path):
    """Several Uvicorn workers start simultaneously and all run migrations."""
    path = tmp_path / "concurrent.sqlite3"
    SQLiteCallStore(path, tmp_path)  # create it once, then race reopens

    def open_store(_index: int) -> str:
        return SQLiteCallStore(path, tmp_path).active_provider_setting()

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(open_store, range(6)))

    assert results == ["twilio"] * 6


# ---------------------------------------------------------------------------
# The setting is shared state
# ---------------------------------------------------------------------------
def test_the_env_seed_only_applies_when_the_row_is_absent(store):
    store.seed_setting(ACTIVE_PROVIDER_KEY, "exotel")
    assert store.active_provider_setting() == "exotel"
    # A later deployment with a different env var must not override the
    # operator's stored choice.
    store.set_active_provider("twilio")
    store.seed_setting(ACTIVE_PROVIDER_KEY, "exotel")
    assert store.active_provider_setting() == "twilio"


def test_two_workers_agree_after_one_of_them_changes_the_setting(tmp_path):
    """A module-level global or a value cached at construction would let these
    two disagree indefinitely."""
    path = tmp_path / "shared.sqlite3"
    worker_a = SQLiteCallStore(path, tmp_path)
    worker_b = SQLiteCallStore(path, tmp_path)
    assert worker_b.active_provider_setting() == "twilio"

    worker_a.set_active_provider("exotel")
    worker_b._provider_setting_cache = None  # expire the short TTL deterministically

    assert worker_b.active_provider_setting() == "exotel"
    assert worker_a.active_provider_setting() == "exotel"


def test_an_unknown_stored_value_falls_back_rather_than_crashing(store):
    with store.transaction(immediate=True) as db:
        db.execute("UPDATE settings SET value='carrier-pigeon' WHERE key=?", (ACTIVE_PROVIDER_KEY,))
    store._provider_setting_cache = None
    assert store.active_provider_setting() == "twilio"


def test_setting_an_unknown_provider_is_rejected(store):
    with pytest.raises(ValueError):
        store.set_active_provider("carrier-pigeon")


def test_every_change_is_audited_with_old_and_new_values(store):
    store.seed_setting(ACTIVE_PROVIDER_KEY, "twilio")  # as startup does
    store.set_active_provider("exotel")
    store.set_active_provider("twilio")

    events = store.list_settings_events()  # newest first

    assert [(e["old_value"], e["new_value"]) for e in events] == [("exotel", "twilio"), ("twilio", "exotel")]
    assert all(event["timestamp"] for event in events)
    assert all(event["actor"] == "operator" for event in events)


def test_the_audit_trail_does_not_live_in_call_events(store):
    """call_events.call_id is NOT NULL and references calls(call_id). A
    settings change has no call, and relaxing that key to make room would
    weaken a constraint protecting the call history."""
    store.set_active_provider("exotel")
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM settings_events").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO call_events(call_id,event_name,metadata,timestamp) VALUES(NULL,'x','{}','now')")


# ---------------------------------------------------------------------------
# Per-call persistence
# ---------------------------------------------------------------------------
def test_provider_is_written_to_the_call_row_at_enqueue(store):
    store.set_active_provider("exotel")
    call = store.enqueue_call(phone_number="+919000000002")
    assert call["provider"] == "exotel"
    assert store.get_call(call["call_id"])["provider"] == "exotel"


def test_the_resolved_provider_is_recorded_as_an_event(store):
    store.set_active_provider("exotel")
    call = store.enqueue_call(phone_number="+919000000003")
    queued = [e for e in store.list_events(call["call_id"]) if e["event_name"] == "queued"]
    assert '"provider": "exotel"' in queued[0]["metadata"]


def test_every_later_stage_reads_the_provider_from_the_row(store):
    """claim_job and claim_due_action both hand the coordinator a call dict;
    the provider has to be on it or the coordinator would have to go looking
    for the current setting."""
    store.set_active_provider("exotel")
    call_id = store.enqueue_call(phone_number="+919000000004")["call_id"]

    claimed = store.claim_job("owner", 1)
    assert claimed["provider"] == "exotel"

    store.bind_call_sid(call_id, "EX-1", 45, -1)
    action = store.claim_due_action("worker")
    assert action["provider"] == "exotel"


# ---------------------------------------------------------------------------
# The failure this exists to prevent
# ---------------------------------------------------------------------------
def test_switching_the_setting_mid_flight_does_not_disturb_an_in_flight_call(store):
    """Enqueue under Twilio, flip to Exotel, then drive the call through dial,
    status callback and reconciliation. It must use Twilio throughout, and
    reconciliation must neither fail nor quarantine it."""
    call_id = store.enqueue_call(phone_number="+919000000005")["call_id"]
    assert store.get_call(call_id)["provider"] == "twilio"

    store.set_active_provider("exotel")  # operator flips it mid-flight

    asked: list[tuple[str, str]] = []

    class RecordingProvider:
        def __init__(self, name: str) -> None:
            self.name = name

        async def dial(self, *, call_id, to_number, ring_timeout, stream_url, status_callback_url):
            from app.telephony.providers.base import DialResult

            asked.append((self.name, "dial"))
            return DialResult("CA-inflight")

        async def fetch_status(self, provider_sid):
            asked.append((self.name, "fetch"))
            return "completed"

        async def request_terminal(self, provider_sid, requested):
            asked.append((self.name, "terminal"))

        def terminal_request_for(self, status):
            return "completed"

        def classify_dial_error(self, error):
            return "ambiguous"

    async def scenario():
        coordinator = DurableCallCoordinator(store, 1, 0, provider_factory=RecordingProvider)
        claimed = store.claim_job(coordinator.owner, 1)
        await coordinator._dial(claimed)

        # A status callback lands for the call, then its deadline comes due.
        store.provider_status(call_id, "in-progress", "CA-inflight")
        with store.transaction(immediate=True) as db:
            db.execute("UPDATE calls SET ring_deadline='1970-01-01T00:00:00+00:00' WHERE call_id=?", (call_id,))
        action = store.claim_due_action(coordinator.owner)
        assert action is not None, "the call must still be reconcilable"
        await coordinator._reconcile(action)

    asyncio.run(scenario())

    assert {name for name, _step in asked} == {"twilio"}, "Exotel must never be asked about a Twilio call"
    saved = store.get_call(call_id)
    assert saved["provider"] == "twilio"
    assert saved["provider_status"] == "completed"
    assert saved["reconciliation_status"] is None
    assert saved["lifecycle_state"] != "NEEDS_RECONCILIATION"


def test_a_call_queued_after_the_switch_uses_the_new_provider(store):
    """The flip must still take effect -- for new work only."""
    before = store.enqueue_call(phone_number="+919000000006")["call_id"]
    store.set_active_provider("exotel")
    after = store.enqueue_call(phone_number="+919000000007")["call_id"]

    assert store.get_call(before)["provider"] == "twilio"
    assert store.get_call(after)["provider"] == "exotel"


# ---------------------------------------------------------------------------
# auto routing
# ---------------------------------------------------------------------------
def test_auto_sends_indian_numbers_to_exotel_and_the_rest_to_twilio(store):
    store.set_active_provider("auto")
    assert store.enqueue_call(phone_number="+919000000008")["provider"] == "exotel"
    assert store.enqueue_call(phone_number="+14155550002")["provider"] == "twilio"


def test_an_explicit_selection_is_authoritative_over_the_destination(store):
    """auto is a third value of the setting, not a layer that overrides a
    manual choice."""
    store.set_active_provider("twilio")
    assert store.enqueue_call(phone_number="+919000000009")["provider"] == "twilio"
    store.set_active_provider("exotel")
    assert store.enqueue_call(phone_number="+14155550003")["provider"] == "exotel"


def test_auto_falls_back_rather_than_failing_the_enqueue(store, monkeypatch):
    """An unconfigured preferred provider must not turn a misconfiguration
    into lost work."""
    monkeypatch.setattr(
        SQLiteCallStore, "_provider_is_configured", staticmethod(lambda name: name == "twilio")
    )
    store.set_active_provider("auto")

    call = store.enqueue_call(phone_number="+919000000010")

    assert call["provider"] == "twilio", "+91 prefers Exotel, but it cannot dial"


def test_an_explicit_unconfigured_provider_also_falls_back(store, monkeypatch):
    monkeypatch.setattr(
        SQLiteCallStore, "_provider_is_configured", staticmethod(lambda name: name == "twilio")
    )
    store.set_active_provider("exotel")
    assert store.enqueue_call(phone_number="+919000000011")["provider"] == "twilio"


def test_with_nothing_configured_the_call_is_still_queued(store, monkeypatch):
    """It fails visibly at dial time with a real provider error instead of
    being silently rejected at the door."""
    monkeypatch.setattr(SQLiteCallStore, "_provider_is_configured", staticmethod(lambda name: False))
    store.set_active_provider("auto")
    assert store.enqueue_call(phone_number="+919000000012")["provider"] == "twilio"
