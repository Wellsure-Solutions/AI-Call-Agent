from __future__ import annotations

import pytest

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


