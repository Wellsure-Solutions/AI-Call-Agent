from __future__ import annotations

"""Finalising a call must not crash, and must not crash *teardown*.

The reported symptom was a TypeError on every browser test call after the
first:

    connected = db.execute("SELECT media_connected ...").fetchone()[0]
    TypeError: 'NoneType' object is not subscriptable

Cause: every browser call was stored under the literal phone number
"browser". Browser calls end in AI_FINISHED, which is not a terminal
lifecycle state, so the `one_active_phone` partial unique index kept the
first row's slot held forever and every later `INSERT OR IGNORE` was
silently skipped -- leaving no row for the SELECT that follows it.
"""

import asyncio
from pathlib import Path

import pytest

from app.core.models import CallSession
from app.storage.sqlite_store import SQLiteCallStore, SuppressedError


def repo(tmp_path: Path) -> SQLiteCallStore:
    return SQLiteCallStore(tmp_path / "browser.db", tmp_path)


def browser_session(call_id: str) -> CallSession:
    session = CallSession(call_id=call_id, direction="browser", metadata={"media_connected": True})
    session.add_turn("user", "hello")
    session.finish("completed")
    return session


# ---------------------------------------------------------------------------
# The reported crash
# ---------------------------------------------------------------------------
def test_repeated_browser_calls_all_persist(tmp_path):
    """Three in a row: before the fix, calls two and three raised TypeError."""
    store = repo(tmp_path)
    for index in range(3):
        call_id = f"browser-{index}"
        store.persist_raw(browser_session(call_id))
        saved = store.get_call(call_id)
        assert saved is not None, f"call {index} was not persisted"
        assert "hello" in saved["transcript"]


def test_each_browser_call_gets_its_own_phone_slot(tmp_path):
    """The shared literal is what collided; distinct values are the fix."""
    store = repo(tmp_path)
    store.persist_raw(browser_session("browser-a"))
    store.persist_raw(browser_session("browser-b"))
    numbers = {store.get_call(c)["phone_number"] for c in ("browser-a", "browser-b")}
    assert len(numbers) == 2
    assert all(number.startswith("browser:") for number in numbers)


def test_a_real_phone_number_is_still_stored_verbatim(tmp_path):
    """The pseudo-phone must not leak into calls that have a real number --
    DND suppression and one-active-call-per-phone both key off this column.
    """
    store = repo(tmp_path)
    session = CallSession(call_id="real-1", phone_number="+919876543210", direction="twilio",
                          metadata={"media_connected": True})
    session.finish("completed")
    store.persist_raw(session)
    assert store.get_call("real-1")["phone_number"] == "+919876543210"


# ---------------------------------------------------------------------------
# When the row genuinely cannot exist
# ---------------------------------------------------------------------------
def test_a_blocked_insert_raises_a_named_error_rather_than_a_typeerror(tmp_path):
    """Still reachable: a second call to a real number whose first call has
    not reached a terminal state. The failure must name itself."""
    store = repo(tmp_path)
    first = CallSession(call_id="dup-1", phone_number="+919876500000", direction="twilio",
                        metadata={"media_connected": True})
    first.finish("completed")
    store.persist_raw(first)

    second = CallSession(call_id="dup-2", phone_number="+919876500000", direction="twilio",
                         metadata={"media_connected": True})
    second.finish("completed")
    with pytest.raises(SuppressedError, match="dup-2"):
        store.persist_raw(second)


def test_a_failed_finalization_does_not_abort_the_rest_of_teardown(tmp_path):
    """AudioBridge.stop() runs from the adapter's `finally`. A raise there
    skipped the media dump close and the metrics flush, so the batch report
    lost every measurement for the call that failed -- and the reason with it.
    """
    from app.telephony.audio.audio_bridge import AudioBridge

    class ExplodingResultService:
        async def afinalize(self, session, status):
            raise SuppressedError("storage is unavailable")

    session = CallSession(call_id="teardown-1", direction="browser")
    bridge = AudioBridge(session, ExplodingResultService())

    asyncio.run(bridge.stop("completed"))

    assert session.ended_at is not None, "the session must still be closed out"
