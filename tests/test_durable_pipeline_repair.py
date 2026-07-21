from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.models import CallSession
from app.services.call_coordinator import DurableCallCoordinator
from app.storage.sqlite_store import SQLiteCallStore, SuppressedError


def repo(tmp_path: Path) -> SQLiteCallStore:
    return SQLiteCallStore(tmp_path / "pipeline.db", tmp_path)


def add_lead(store: SQLiteCallStore, number: str) -> dict:
    return store.import_leads([{"business_name": "Acme", "phone_number": number, "category": "Retail", "notes": ""}])["leads"][0]


def enqueue(store: SQLiteCallStore, number: str) -> dict:
    lead = add_lead(store, number)
    return store.enqueue_call(phone_number=lead["phone_number"], lead_id=lead["lead_id"])


def past() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()


def test_expired_claim_is_quarantined_and_does_not_deadlock_capacity_one(tmp_path):
    store = repo(tmp_path)
    first = enqueue(store, "+14155550001")
    second = enqueue(store, "+14155550002")
    assert store.claim_job("crashed", 1)["call_id"] == first["call_id"]
    with store.transaction(True) as db:
        db.execute("UPDATE call_jobs SET lease_expires_at=? WHERE call_id=?", (past(), first["call_id"]))
    claimed = store.claim_job("replacement", 1)
    assert claimed["call_id"] == second["call_id"]
    assert store.get_call(first["call_id"])["lifecycle_state"] == "NEEDS_RECONCILIATION"


def test_expired_ambiguous_job_is_never_redialed_and_phone_remains_blocked(tmp_path):
    store = repo(tmp_path)
    call = enqueue(store, "+14155550003")
    store.claim_job("crashed", 2)
    with store.transaction(True) as db:
        db.execute("UPDATE call_jobs SET lease_expires_at=? WHERE call_id=?", (past(), call["call_id"]))
    store.claim_job("other", 2)
    same = store.enqueue_call(phone_number=call["phone_number"])
    assert same["call_id"] == call["call_id"]
    with store.transaction() as db:
        assert db.execute("SELECT queue_state FROM call_jobs WHERE call_id=?", (call["call_id"],)).fetchone()[0] == "needs_reconciliation"


def test_late_status_binds_original_sid_and_terminal_releases_capacity(tmp_path):
    store = repo(tmp_path)
    call = enqueue(store, "+14155550004")
    store.claim_job("crashed", 1)
    with store.transaction(True) as db:
        db.execute("UPDATE call_jobs SET lease_expires_at=? WHERE call_id=?", (past(), call["call_id"]))
    store.claim_job("recovery", 1)
    store.provider_status(call["call_id"], "ringing", "CA-late")
    assert store.get_call(call["call_id"])["call_sid"] == "CA-late"
    store.provider_status(call["call_id"], "completed", "CA-late")
    with store.transaction() as db:
        assert db.execute("SELECT queue_state FROM call_jobs WHERE call_id=?", (call["call_id"],)).fetchone()[0] == "terminal"


def test_raw_and_provider_finalization_are_order_independent(tmp_path):
    store = repo(tmp_path)
    one = enqueue(store, "+14155550005")
    store.bind_call_sid(one["call_id"], "CA-one")
    store.claim_media(one["call_id"], "CA-one", "media")
    session = CallSession(call_id=one["call_id"], phone_number=one["phone_number"], direction="twilio", metadata={"media_connected": True})
    session.add_turn("user", "hello")
    session.finish("completed")
    store.persist_raw(session)
    assert store.get_call(one["call_id"])["provider_terminal_at"] is None
    store.provider_status(one["call_id"], "completed", "CA-one")
    saved = store.get_call(one["call_id"])
    assert saved["provider_status"] == "completed" and "hello" in saved["transcript"] and saved["finalized_at"]

    two = enqueue(store, "+14155550006")
    store.bind_call_sid(two["call_id"], "CA-two")
    store.provider_status(two["call_id"], "completed", "CA-two")
    session.call_id, session.phone_number = two["call_id"], two["phone_number"]
    store.persist_raw(session)
    assert store.get_call(two["call_id"])["provider_status"] == "completed"
    assert "hello" in store.get_call(two["call_id"])["transcript"]


def test_delayed_nonterminal_status_cannot_regress_terminal_and_media_error_is_preserved(tmp_path):
    store = repo(tmp_path)
    call = enqueue(store, "+14155550007")
    store.bind_call_sid(call["call_id"], "CA-error")
    store.claim_media(call["call_id"], "CA-error", "media")
    session = CallSession(call_id=call["call_id"], phone_number=call["phone_number"], direction="twilio", metadata={"media_connected": True})
    session.finish("error")
    store.persist_raw(session, "ai_disconnected")
    store.provider_status(call["call_id"], "completed", "CA-error")
    finalized = store.get_call(call["call_id"])["provider_terminal_at"]
    store.provider_status(call["call_id"], "ringing", "CA-error")
    saved = store.get_call(call["call_id"])
    assert saved["provider_status"] == "completed"
    assert saved["lifecycle_state"] == "FAILED" and saved["outcome"] == "ai_disconnected"
    assert saved["provider_terminal_at"] == finalized


def test_extraction_claim_is_single_owner_retry_delay_and_restart_safe(tmp_path):
    store = repo(tmp_path)
    call = enqueue(store, "+14155550008")
    session = CallSession(call_id=call["call_id"], phone_number=call["phone_number"], direction="twilio", metadata={"media_connected": True})
    session.finish()
    store.persist_raw(session, max_extraction_attempts=2)
    other = SQLiteCallStore(store.database_path, tmp_path)
    claimed = store.claim_extraction("worker-a", 60)
    assert claimed and other.claim_extraction("worker-b", 60) is None
    assert store.fail_extraction(call["call_id"], "worker-a", "RateLimitError", 30)
    with store.transaction() as db:
        job = db.execute("SELECT * FROM extraction_jobs WHERE call_id=?", (call["call_id"],)).fetchone()
        assert job["state"] == "retry_pending" and job["next_attempt_at"] > job["updated_at"]
    assert other.claim_extraction("worker-b", 60) is None


def test_permanent_extraction_failure_marks_connected_lead_review(tmp_path):
    store = repo(tmp_path)
    lead = add_lead(store, "+14155550009")
    call = store.enqueue_call(phone_number=lead["phone_number"], lead_id=lead["lead_id"])
    session = CallSession(call_id=call["call_id"], phone_number=call["phone_number"], direction="twilio", metadata={"lead_id": lead["lead_id"], "media_connected": True})
    session.finish(); store.persist_raw(session, max_extraction_attempts=1)
    store.claim_extraction("worker", 60)
    store.fail_extraction(call["call_id"], "worker", "InvalidResponse", 1)
    saved = store.get_lead(lead["lead_id"])
    assert saved["review_required"] == 1 and saved["status"] == "review_required"


def test_deadline_action_has_single_owner_and_capacity_waits_for_terminal(tmp_path):
    store = repo(tmp_path)
    call = enqueue(store, "+14155550010")
    store.claim_job("dialer", 1); store.bind_call_sid(call["call_id"], "CA-deadline", 45, 900)
    with store.transaction(True) as db:
        db.execute("UPDATE calls SET ring_deadline=? WHERE call_id=?", (past(), call["call_id"]))
    assert store.claim_due_action("worker-a") is not None
    assert SQLiteCallStore(store.database_path, tmp_path).claim_due_action("worker-b") is None
    assert store.claim_job("dialer-two", 1) is None
    store.reconciliation_result(call["call_id"], "worker-a", None, "cancel requested")
    assert store.claim_job("dialer-two", 1) is None
    store.provider_status(call["call_id"], "canceled", "CA-deadline")


def test_historical_dnd_v2_migration_and_malformed_json_are_safe(tmp_path):
    store = repo(tmp_path)
    lead = add_lead(store, "+14155550011")
    call = store.enqueue_call(phone_number=lead["phone_number"], lead_id=lead["lead_id"])
    with store.transaction(True) as db:
        db.execute("UPDATE calls SET lifecycle_state='COMPLETED',structured_response=? WHERE call_id=?", (json.dumps({"account_creation_interest": "maybe", "do_not_call_requested": "yes"}), call["call_id"]))
        db.execute("INSERT INTO calls(call_id,phone_number,lifecycle_state,structured_response,created_at,updated_at) VALUES('bad','+14155559999','COMPLETED','{bad',?,?)", (past(), past()))
        db.execute("DELETE FROM schema_metadata WHERE key='legacy_dnd_v2'")
    SQLiteCallStore(store.database_path, tmp_path)
    SQLiteCallStore(store.database_path, tmp_path)
    assert store.get_lead(lead["lead_id"])["do_not_call"] == 1
    try:
        store.enqueue_call(phone_number=lead["phone_number"], lead_id=lead["lead_id"])
    except SuppressedError:
        pass
    else:
        raise AssertionError("historical DND was callable")


def test_complete_exports_and_statistics_include_more_than_dashboard_limit(tmp_path):
    store = repo(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    with store.transaction(True) as db:
        for index in range(201):
            db.execute("""INSERT INTO calls(call_id,phone_number,lifecycle_state,outcome,structured_response,interested,created_at,updated_at)
                VALUES(?,?,'COMPLETED','completed',?,1,?,?)""", (f"call-{index}", f"+1416{index:010d}", json.dumps({"account_creation_interest": "maybe"}), now, now))
    payload = json.loads(store.export_calls("json")[1])
    assert len(payload) == 201 and isinstance(payload[0]["structured_response"], dict)
    assert store.statistics()["total_calls"] == 201


def test_compatibility_tracker_has_hard_call_and_event_bounds():
    from app.services.call_status_tracker import CallStatusTracker
    tracker = CallStatusTracker(max_calls=3, max_events_per_call=2)
    for index in range(5):
        for event in range(4):
            tracker.upsert(str(index), str(event))
    assert len(tracker.list()) == 3
    assert all(len(item["events"]) == 2 for item in tracker.list())


class ResilientStore:
    def __init__(self): self.claims = 0
    def claim_due_action(self, owner):
        self.claims += 1
        if self.claims == 1: raise RuntimeError("database busy")
        return None
    def claim_extraction(self, owner, lease): return None
    def claim_job(self, owner, maximum): return None


def test_coordinator_iteration_exception_does_not_kill_loop():
    async def scenario():
        store = ResilientStore()
        coordinator = DurableCallCoordinator(store, 1, 0)
        task = asyncio.create_task(coordinator.run())
        await asyncio.sleep(.4)
        coordinator.stop(); await task
        assert store.claims >= 2 and coordinator.iterations >= 2
    asyncio.run(scenario())

def test_maximum_call_deadline_is_durable_and_claimed(tmp_path):
    store = repo(tmp_path)
    call = enqueue(store, "+14155550012")
    store.claim_job("dialer", 1); store.bind_call_sid(call["call_id"], "CA-max")
    store.claim_media(call["call_id"], "CA-max", "media")
    with store.transaction(True) as db:
        db.execute("UPDATE calls SET max_call_deadline=? WHERE call_id=?", (past(), call["call_id"]))
    action = store.claim_due_action("deadline-worker")
    assert action and action["call_id"] == call["call_id"]
    assert action["action_owner"] == "deadline-worker"


class FakeProviderAdapter:
    statuses = []
    updates = []
    def __init__(self): pass
    async def fetch_status(self, sid): return self.statuses.pop(0)
    async def update_status(self, sid, status): self.updates.append((sid, status))


def test_lost_terminal_webhook_is_recovered_by_provider_lookup(tmp_path):
    async def scenario():
        store = repo(tmp_path)
        call = enqueue(store, "+14155550013")
        store.claim_job("dialer", 1); store.bind_call_sid(call["call_id"], "CA-lookup")
        with store.transaction(True) as db:
            db.execute("UPDATE calls SET ring_deadline=? WHERE call_id=?", (past(), call["call_id"]))
        FakeProviderAdapter.statuses = ["completed"]
        coordinator = DurableCallCoordinator(store, 1, 0, adapter_factory=FakeProviderAdapter)
        action = store.claim_due_action(coordinator.owner)
        await coordinator._reconcile(action)
        saved = store.get_call(call["call_id"])
        assert saved["provider_status"] == "completed" and saved["provider_terminal_at"]
    asyncio.run(scenario())
