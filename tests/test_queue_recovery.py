from __future__ import annotations

import asyncio

import pytest

from app.services.call_coordinator import DurableCallCoordinator
from app.storage.sqlite_store import SQLiteCallStore


@pytest.fixture()
def store(tmp_path) -> SQLiteCallStore:
    return SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)


def active_call(store: SQLiteCallStore, phone: str, sid: str, *, max_seconds: int = 900) -> str:
    """A call that has been dialled, bound, and is occupying a capacity slot."""
    call_id = store.enqueue_call(phone_number=phone)["call_id"]
    store.claim_job("owner", 10)
    store.bind_call_sid(call_id, sid, 45, max_seconds)
    return call_id


def queue_state(store: SQLiteCallStore, call_id: str) -> str:
    return store._one("SELECT queue_state FROM call_jobs WHERE call_id=?", (call_id,))["queue_state"]


def capacity(store: SQLiteCallStore) -> int:
    return store.capacity_snapshot()["capacity_occupied"]


def exhaust_reconciliation(store: SQLiteCallStore, attempts: int) -> None:
    """Drive repeated provider-lookup failures.

    Backoff is skipped by hand rather than slept through: the retry delay is
    not what this is testing, and the real schedule reaches five minutes.
    """
    for _ in range(attempts):
        claimed = store.claim_due_action("worker")
        if claimed is None:
            break
        store.reconciliation_result(claimed["call_id"], "worker", None, "ConnectionError", attempts)
        with store.transaction(immediate=True) as db:
            db.execute("UPDATE calls SET next_reconciliation_at=? WHERE next_reconciliation_at IS NOT NULL",
                       ("1970-01-01T00:00:00+00:00",))


# ---------------------------------------------------------------------------
# Empty-database statistics
# ---------------------------------------------------------------------------
def test_statistics_on_a_fresh_database_does_not_crash(store):
    """SUM() over zero rows is NULL, not 0. The dashboard 500'd on first load,
    before a single call had been placed."""
    stats = store.statistics()
    assert stats["total_calls"] == 0
    assert stats["calls_answered"] == 0
    assert stats["calls_not_answered"] == 0
    assert stats["interested_responses"] == 0
    assert stats["success_rate"] == 0


def test_statistics_still_counts_real_calls(store):
    call_id = active_call(store, "+919000000001", "CA" + "1" * 32)
    store.provider_status(call_id, "completed")
    stats = store.statistics()
    assert stats["total_calls"] == 1


# ---------------------------------------------------------------------------
# Bounded reconciliation: an unreachable provider must not hold a slot forever
# ---------------------------------------------------------------------------
def test_repeated_provider_failures_eventually_release_capacity(store):
    """The stall this fixes: reconciliation_result put the job straight back
    to 'active' on every failure, so a provider that could not be reached at
    all retried every 300s forever while holding the only capacity slot."""
    call_id = active_call(store, "+919000000002", "CA" + "2" * 32, max_seconds=-1)
    assert capacity(store) == 1

    exhaust_reconciliation(store, attempts=8)

    assert queue_state(store, call_id) == "needs_reconciliation"
    assert capacity(store) == 0, "a call nobody can resolve must stop blocking the queue"


def test_a_quarantined_call_is_never_terminalized_or_redialed(store):
    """Releasing capacity must not invent an outcome. The call stays
    unresolved, operator-visible, and its phone stays blocked."""
    call_id = active_call(store, "+919000000003", "CA" + "3" * 32, max_seconds=-1)
    exhaust_reconciliation(store, attempts=8)

    call = store.get_call(call_id)
    assert call["provider_terminal_at"] is None, "no outcome may be invented"
    assert call["lifecycle_state"] == "NEEDS_RECONCILIATION"
    assert any(c["call_id"] == call_id for c in store.list_reconciliation())
    # Same phone must not be dialable again while unresolved.
    again = store.enqueue_call(phone_number="+919000000003")
    assert again["call_id"] == call_id


def test_a_provable_terminal_status_still_resolves_normally(store):
    """The bound must not short-circuit the normal path."""
    call_id = active_call(store, "+919000000004", "CA" + "4" * 32, max_seconds=-1)
    claimed = store.claim_due_action("worker")
    store.reconciliation_result(claimed["call_id"], "worker", "completed", None, 8)

    assert store.get_call(call_id)["provider_terminal_at"] is not None
    assert queue_state(store, call_id) == "terminal"
    assert capacity(store) == 0


def test_failures_below_the_bound_keep_retrying(store):
    call_id = active_call(store, "+919000000005", "CA" + "5" * 32, max_seconds=-1)
    claimed = store.claim_due_action("worker")
    store.reconciliation_result(claimed["call_id"], "worker", None, "Timeout", 8)

    assert queue_state(store, call_id) == "active", "one failure must not quarantine"
    assert capacity(store) == 1


# ---------------------------------------------------------------------------
# The sweeper: calls no deadline action can even see
# ---------------------------------------------------------------------------
def test_a_call_past_every_deadline_is_swept_out_of_capacity(store):
    call_id = active_call(store, "+919000000006", "CA" + "6" * 32, max_seconds=-4000)
    assert capacity(store) == 1

    assert store.quarantine_abandoned_jobs(grace_seconds=300) == 1
    assert queue_state(store, call_id) == "needs_reconciliation"
    assert capacity(store) == 0


def test_a_live_call_inside_its_window_is_left_alone(store):
    """The sweeper must never touch a call that could still be talking."""
    active_call(store, "+919000000007", "CA" + "7" * 32, max_seconds=900)
    assert store.quarantine_abandoned_jobs(grace_seconds=300) == 0
    assert capacity(store) == 1


def test_the_sweeper_never_touches_a_resolved_call(store):
    call_id = active_call(store, "+919000000008", "CA" + "8" * 32, max_seconds=-4000)
    store.provider_status(call_id, "completed")
    assert store.quarantine_abandoned_jobs(grace_seconds=300) == 0
    assert queue_state(store, call_id) == "terminal"


def test_capacity_freed_by_a_sweep_lets_the_next_call_start(store):
    """The whole point: a queued call must actually begin afterwards."""
    stuck = active_call(store, "+919000000009", "CA" + "9" * 32, max_seconds=-4000)
    waiting = store.enqueue_call(phone_number="+919000000010")["call_id"]
    assert store.claim_job("owner", 1) is None, "blocked while the stuck call holds the slot"

    store.quarantine_abandoned_jobs(grace_seconds=300)
    claimed = store.claim_job("owner", 1)

    assert claimed is not None and claimed["call_id"] == waiting
    assert queue_state(store, stuck) == "needs_reconciliation"


# ---------------------------------------------------------------------------
# Operator override
# ---------------------------------------------------------------------------
def test_an_operator_can_resolve_a_call_the_provider_never_will(store):
    call_id = active_call(store, "+919000000011", "CA" + "A" * 32, max_seconds=-4000)
    store.quarantine_abandoned_jobs(grace_seconds=300)

    resolved = store.resolve_stuck_call(call_id, "failed", "checked Twilio console")

    assert resolved["provider_status"] == "failed"
    assert resolved["provider_terminal_at"] is not None
    assert queue_state(store, call_id) == "terminal"
    events = {row["event_name"] for row in store.list_events(call_id)}
    assert "operator_resolved" in events, "a human decision must be recorded as one"


def test_resolving_rejects_a_status_the_provider_would_never_report(store):
    call_id = active_call(store, "+919000000012", "CA" + "B" * 32)
    with pytest.raises(ValueError):
        store.resolve_stuck_call(call_id, "made-up-status")


def test_resolving_an_already_terminal_call_is_a_no_op(store):
    call_id = active_call(store, "+919000000013", "CA" + "C" * 32)
    store.provider_status(call_id, "completed")
    resolved = store.resolve_stuck_call(call_id, "failed")
    assert resolved["provider_status"] == "completed", "a real outcome must not be overwritten"


def test_resolving_an_unknown_call_reports_not_found(store):
    assert store.resolve_stuck_call("no-such-call") is None


# ---------------------------------------------------------------------------
# The coordinator must survive a failing sweep
# ---------------------------------------------------------------------------
def test_a_failing_sweep_does_not_stop_the_coordinator_dialling(store, monkeypatch):
    """Housekeeping that fails the whole iteration would reproduce the stall
    it exists to clear, by a different route."""
    def explode(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "quarantine_abandoned_jobs", explode)
    store.enqueue_call(phone_number="+919000000014")
    dialled: list[str] = []

    class Adapter:
        call_sid = "CA" + "D" * 32
        ring_timeout = 45

        def attach(self, session):
            self.session = session

        async def connect(self):
            dialled.append(self.session.call_id)

    async def scenario():
        coordinator = DurableCallCoordinator(store, 1, 0, adapter_factory=Adapter)
        await coordinator.run_once()

    asyncio.run(scenario())
    assert dialled, "a broken sweep must not prevent the queue from dialling"
