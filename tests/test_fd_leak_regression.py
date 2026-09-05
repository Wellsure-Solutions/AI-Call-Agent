"""Regression tests for the production file-descriptor exhaustion incident.

Production hit its 1024 open-file limit after a few hours, which surfaced as
`sqlite3.OperationalError: unable to open database file` inside
`claim_due_action` and, once the process could no longer accept a new socket
either, as Caddy 502s on every route.

Root cause: `sqlite3.Connection.__exit__` only commits or rolls back the
current transaction -- by design it "neither implicitly opens a new
transaction nor closes the connection". `SQLiteCallStore._rows`, `_one`,
`capacity_snapshot`, and `statistics` all used `with self._connect() as db:`,
so every one of those calls opened a connection (three file descriptors
under WAL: the main db file, `-wal`, and `-shm`) and never closed it. Nothing
raised: the leaked connection is referenced only by a reference cycle inside
the sqlite3 module's own statement-cache bookkeeping, so plain refcounting
never reclaims it -- only a cyclic-GC pass does, and under sustained request
volume (`/api/calls`, `/api/live-calls`, `/api/stats`, `/api/leads`, the
coordinator's reconciliation loop) leaks accumulated faster than that.

Secondary contributor: `get_provider("twilio")` builds a fresh
`TwilioProvider` for every dial and every reconciliation attempt, and its
`Client` (and the `TwilioAdapter` built for every inbound media WebSocket)
each wrapped a brand-new, never-closed `requests.Session` -- the Twilio SDK's
own persistent HTTP connection pool -- because nothing shared or closed it.

These tests reproduce the leak under the *unpatched* code (see the first
test's docstring for how to check that) and pin the fix: reads must not grow
the process's open-file count, and both Twilio entry points must share one
client instead of building one per call.
"""

from __future__ import annotations

import os

import pytest

from app.storage.sqlite_store import SQLiteCallStore

pytestmark = pytest.mark.skipif(
    not os.path.isdir("/proc/self/fd"),
    reason="file-descriptor accounting via /proc is Linux-only",
)


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _store(tmp_path):
    return SQLiteCallStore(tmp_path / "calls.db", tmp_path)


def _lead(repo, phone: str = "+14155552671"):
    return repo.import_leads(
        [{"business_name": "Acme", "phone_number": phone, "category": "Retail", "notes": ""}]
    )["leads"][0]


def test_repeated_reads_do_not_leak_file_descriptors(tmp_path):
    """The read surface every dashboard/API route depends on must not leak.

    Deliberately never calls `gc.collect()`: a real request handler never
    does either, so this reproduces the actual production growth rather than
    papering over it with a collection the code path itself never performs.
    Reverting the `_read_connection` fix (i.e. going back to
    `with self._connect() as db:`) makes this fail by a wide margin --
    dropping the fix locally showed several hundred descriptors leaked over
    the same 300 iterations.
    """
    repo = _store(tmp_path)
    item = _lead(repo)
    call = repo.enqueue_call(phone_number=item["phone_number"], lead_id=item["lead_id"])

    baseline = _open_fd_count()
    for _ in range(300):
        repo.list_calls()
        repo.get_call(call["call_id"])
        repo.list_leads()
        repo.get_lead(item["lead_id"])
        repo.list_live_calls()
        repo.statistics()
        repo.capacity_snapshot()

    grown = _open_fd_count() - baseline
    assert grown <= 5, (
        f"open file descriptors grew by {grown} over 300 read-only calls with "
        "no gc.collect() -- a read helper is leaking a sqlite3 connection"
    )


def test_read_connection_closes_on_success_and_on_exception(tmp_path):
    """Unit-level pin on the exact mechanism, so the next reader who touches
    `_read_connection` cannot reintroduce `with self._connect() as db:` --
    which compiles, runs, and passes every functional test, because a
    `sqlite3.Connection` used as a context manager silently commits instead
    of closing."""
    repo = _store(tmp_path)

    baseline = _open_fd_count()
    with repo._read_connection() as db:
        db.execute("SELECT 1")
    assert _open_fd_count() == baseline

    with pytest.raises(RuntimeError):
        with repo._read_connection() as db:
            db.execute("SELECT 1")
            raise RuntimeError("boom")
    assert _open_fd_count() == baseline


def test_call_lifecycle_many_times_stays_at_a_stable_descriptor_count(tmp_path):
    """A simulated batch of calls -- enqueue, dial-bind, terminal status,
    reconciliation-shaped reads -- must return to baseline, not climb."""
    repo = _store(tmp_path)

    baseline = _open_fd_count()
    for i in range(150):
        item = _lead(repo, phone=f"+1415555{i:04d}")
        call = repo.enqueue_call(phone_number=item["phone_number"], lead_id=item["lead_id"])
        repo.bind_call_sid(call["call_id"], f"CA{i}", ring_seconds=30, max_seconds=60)
        repo.provider_status(call["call_id"], "completed", f"CA{i}")
        repo.get_call(call["call_id"])
        repo.list_reconciliation()

    grown = _open_fd_count() - baseline
    assert grown <= 5, f"open file descriptors grew by {grown} over 150 simulated call lifecycles"


# ---------------------------------------------------------------------------
# Twilio client lifecycle: shared, not rebuilt (and its own connection pool
# leaked) on every dial, reconciliation attempt, or inbound media socket.
# ---------------------------------------------------------------------------


@pytest.fixture
def _shared_twilio_client(monkeypatch):
    """A clean singleton with deterministic fake credentials, so this file
    never depends on real Twilio env vars and never leaves a client behind
    for other test modules. Not autouse: only the Twilio-specific tests below
    need it, and requesting it explicitly keeps the SQLite tests above able to
    fail on their own against unpatched code (a module-wide autouse fixture
    referencing a not-yet-existing `_shared_client` attribute would error
    every test in this file, which would mask that these are two independent
    fixes)."""
    from app.telephony.providers import twilio_provider

    monkeypatch.setattr(twilio_provider, "_shared_client", None)
    monkeypatch.setattr(twilio_provider, "TWILIO_ACCOUNT_SID", "AC_test_sid")
    monkeypatch.setattr(twilio_provider, "TWILIO_AUTH_TOKEN", "test_auth_token")
    yield
    monkeypatch.setattr(twilio_provider, "_shared_client", None)


def test_twilio_provider_reuses_one_client_across_instances(_shared_twilio_client):
    """`get_provider("twilio")` builds a fresh `TwilioProvider` per dial and
    per reconciliation attempt by design (see `get_provider`'s docstring) --
    but that must no longer mean a fresh, never-closed `requests.Session`
    each time."""
    from app.telephony.providers.twilio_provider import TwilioProvider, get_shared_twilio_client

    first = TwilioProvider()._client
    second = TwilioProvider()._client
    assert first is second, "each fresh TwilioProvider built its own HTTP client/session"
    assert first is get_shared_twilio_client()


def test_twilio_adapter_reuses_the_same_shared_client(_shared_twilio_client):
    """`TwilioAdapter` is constructed fresh for every inbound Twilio media
    WebSocket -- one per call -- and used to build its own `Client()` eagerly
    in `__init__`, unconditionally, even though production never calls
    `connect()` on it (dialling goes through `TwilioProvider`)."""
    from app.telephony.adapters.twilio_adapter import TwilioAdapter
    from app.telephony.providers.twilio_provider import get_shared_twilio_client

    one = TwilioAdapter()
    two = TwilioAdapter()
    assert one._client is two._client
    assert one._client is get_shared_twilio_client()


def test_twilio_provider_still_honors_an_explicit_client(_shared_twilio_client):
    """Tests (and any future caller) that inject a fake/mock client must keep
    getting exactly that client, never the shared singleton."""
    from app.telephony.providers.twilio_provider import TwilioProvider

    sentinel = object()
    assert TwilioProvider(client=sentinel)._client is sentinel


# ---------------------------------------------------------------------------
# Diagnostics: the low-frequency instrumentation added alongside the fix.
# ---------------------------------------------------------------------------


def test_open_fd_count_returns_a_sane_positive_number():
    from app.core.diagnostics import open_fd_count

    count = open_fd_count()
    assert count is not None and count > 0


def test_app_starts_and_stops_cleanly_with_fd_diagnostics_task(monkeypatch):
    """The periodic diagnostics task added to `lifespan` must not keep the
    app from shutting down cleanly -- a background task left uncancelled on
    shutdown is its own, smaller version of this incident's category of bug."""
    from fastapi.testclient import TestClient

    import app.main as main

    monkeypatch.setattr(main, "FD_DIAGNOSTICS_ENABLED", True)
    monkeypatch.setattr(main, "FD_DIAGNOSTICS_INTERVAL_SECONDS", 0.01)

    with TestClient(main.app, raise_server_exceptions=False) as probe:
        probe.get("/")
    # Exiting the `with` block runs the lifespan's shutdown half; reaching
    # here without hanging or raising is the assertion.
