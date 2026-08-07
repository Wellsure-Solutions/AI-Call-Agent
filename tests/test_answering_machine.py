from __future__ import annotations

import json

import pytest

from app.core import settings
from app.storage.sqlite_store import SQLiteCallStore
from app.telephony.adapters.twilio_adapter import TwilioAdapter
from app.telephony.call_session import CallSession


class FakeCalls:
    def __init__(self) -> None:
        self.created: dict = {}

    def create(self, **kwargs):
        self.created = kwargs
        return type("Call", (), {"sid": "CA" + "0" * 32})()


class FakeClient:
    def __init__(self) -> None:
        self.calls = FakeCalls()


def place_call() -> dict:
    client = FakeClient()
    adapter = TwilioAdapter(client=client)
    adapter.attach(CallSession(call_id="call-amd", phone_number="+919000000001", direction="twilio"))
    import asyncio

    asyncio.run(adapter.connect())
    return client.calls.created


def test_detection_runs_asynchronously_so_humans_do_not_wait_for_it():
    """Synchronous AMD makes Twilio hold the call before running our TwiML.
    With a 2400ms default speech threshold that alone exceeds the 500ms
    answer-to-greeting target, on every human call, to catch the machines.
    """
    created = place_call()
    assert created["machine_detection"] == settings.AMD_MODE
    assert created["async_amd"] == "true"
    assert created["async_amd_status_callback"].endswith("/twilio/amd/call-amd")


def test_detection_tuning_is_passed_through_explicitly():
    created = place_call()
    assert created["machine_detection_timeout"] == settings.AMD_TIMEOUT_SECONDS
    assert created["machine_detection_speech_threshold"] == settings.AMD_SPEECH_THRESHOLD_MS
    assert created["machine_detection_speech_end_threshold"] == settings.AMD_SPEECH_END_THRESHOLD_MS
    assert created["machine_detection_silence_timeout"] == settings.AMD_SILENCE_TIMEOUT_MS


def test_the_ring_timeout_and_stream_urls_are_unchanged_by_detection():
    created = place_call()
    assert created["timeout"] == 45
    assert created["url"].endswith("/twilio/twiml/call-amd")
    assert created["status_callback"].endswith("/twilio/status/call-amd")


def test_unknown_is_not_treated_as_a_machine():
    """Detection failing is not evidence of a machine. Hanging up on
    `unknown` would drop real customers on hard-to-classify lines."""
    assert "unknown" not in settings.AMD_TERMINAL_VERDICTS
    assert "human" not in settings.AMD_TERMINAL_VERDICTS
    assert {"machine_start", "fax"} <= settings.AMD_TERMINAL_VERDICTS


def test_the_verdict_is_recorded_without_terminalizing_the_call(tmp_path):
    """AMD is a provider opinion delivered mid-call, not a proven terminal
    status. Capacity must stay held until a real terminal webhook arrives --
    that invariant is the reason the queue can be trusted at all.
    """
    store = SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)
    call_id = store.enqueue_call(phone_number="+919000000003")["call_id"]
    store.claim_job("owner", 1)
    store.bind_call_sid(call_id, "CA" + "1" * 32)

    updated = store.record_answered_by(call_id, "machine_start", "CA" + "1" * 32)

    assert updated["answered_by"] == "machine_start"
    assert updated["provider_terminal_at"] is None
    assert updated["lifecycle_state"] != "COMPLETED"
    job = store._one("SELECT queue_state FROM call_jobs WHERE call_id=?", (call_id,))
    assert job["queue_state"] == "active", "the slot must stay held until a terminal provider status"


def test_a_redelivered_verdict_does_not_overwrite_the_first(tmp_path):
    store = SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)
    call_id = store.enqueue_call(phone_number="+919000000004")["call_id"]
    store.claim_job("owner", 1)
    sid = "CA" + "2" * 32
    store.bind_call_sid(call_id, sid)

    store.record_answered_by(call_id, "machine_start", sid)
    again = store.record_answered_by(call_id, "human", sid)

    assert again["answered_by"] == "machine_start"


def test_a_verdict_for_the_wrong_sid_is_rejected(tmp_path):
    store = SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)
    call_id = store.enqueue_call(phone_number="+919000000005")["call_id"]
    store.claim_job("owner", 1)
    store.bind_call_sid(call_id, "CA" + "3" * 32)

    assert store.record_answered_by(call_id, "machine_start", "CA" + "9" * 32) is None


def test_an_empty_verdict_is_ignored(tmp_path):
    store = SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)
    call_id = store.enqueue_call(phone_number="+919000000006")["call_id"]
    assert store.record_answered_by(call_id, "") is None


def test_the_verdict_lands_in_the_call_event_log(tmp_path):
    store = SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)
    call_id = store.enqueue_call(phone_number="+919000000007")["call_id"]
    store.claim_job("owner", 1)
    sid = "CA" + "4" * 32
    store.bind_call_sid(call_id, sid)
    store.record_answered_by(call_id, "machine_end_beep", sid)

    events = store.list_events(call_id, "answered_by")
    assert json.loads(events[0]["metadata"])["answered_by"] == "machine_end_beep"
