from __future__ import annotations

"""Exotel's webhooks: authentication, status mapping, and correlation.

Exotel signs nothing, so `/exotel/status/{call_id}` is only as safe as the
HMAC token we minted for it. These tests are the equivalent of Twilio's
signature checks, plus the two-halves correlation the media socket does
because its token cannot cover the CallSid.
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.storage.sqlite_store import SQLiteCallStore
from app.telephony import exotel_routes, twilio_routes
from app.telephony.callback_urls import exotel_callback_token, exotel_stream_token

SECRET = "test-secret-not-real"


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setattr("app.telephony.callback_urls.STREAM_SECRET", SECRET)
    monkeypatch.setattr(exotel_routes, "EXOTEL_CALLBACK_ALLOWED_IPS", ())
    twilio_routes._signature_failures.update(total=0, by_endpoint={}, last_at=None, last_endpoint=None)
    yield


@pytest.fixture()
def store(tmp_path) -> SQLiteCallStore:
    built = SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)
    exotel_routes.configure(built, None)
    return built


@pytest.fixture()
def client(store) -> TestClient:
    app = FastAPI()
    app.include_router(exotel_routes.router)
    return TestClient(app)


def exotel_call(store: SQLiteCallStore, phone: str, sid: str) -> str:
    call_id = store.enqueue_call(phone_number=phone, provider="exotel")["call_id"]
    store.claim_job("owner", 10)
    store.bind_call_sid(call_id, sid, 45, 900)
    return call_id


def signed(call_id: str, ttl: int = 3600) -> dict[str, str]:
    expiry = int(time.time()) + ttl
    return {"expiry": str(expiry), "token": exotel_callback_token(call_id, expiry)}


# ---------------------------------------------------------------------------
# Callback authentication
# ---------------------------------------------------------------------------
def test_a_correctly_signed_callback_is_accepted(client, store):
    call_id = exotel_call(store, "+919000001001", "EX-1")

    response = client.post(f"/exotel/status/{call_id}", params=signed(call_id),
                           data={"CallSid": "EX-1", "Status": "completed"})

    assert response.status_code == 200
    assert store.get_call(call_id)["provider_status"] == "completed"


def test_a_callback_with_a_bad_token_is_rejected_and_counted(client, store):
    call_id = exotel_call(store, "+919000001002", "EX-2")
    params = signed(call_id) | {"token": "0" * 64}

    response = client.post(f"/exotel/status/{call_id}", params=params,
                           data={"CallSid": "EX-2", "Status": "completed"})

    assert response.status_code == 403
    assert store.get_call(call_id)["provider_status"] != "completed"
    health = twilio_routes.callback_auth_failure_health()
    assert health["total"] == 1
    assert health["by_endpoint"] == {"exotel_status": 1}


def test_a_callback_with_no_token_at_all_is_rejected(client, store):
    call_id = exotel_call(store, "+919000001003", "EX-3")
    assert client.post(f"/exotel/status/{call_id}", data={"CallSid": "EX-3", "Status": "completed"}).status_code == 403
    assert twilio_routes.callback_auth_failure_health()["total"] == 1


def test_an_expired_callback_token_is_rejected(client, store):
    call_id = exotel_call(store, "+919000001004", "EX-4")
    expiry = int(time.time()) - 1
    params = {"expiry": str(expiry), "token": exotel_callback_token(call_id, expiry)}

    assert client.post(f"/exotel/status/{call_id}", params=params,
                       data={"CallSid": "EX-4", "Status": "completed"}).status_code == 403


def test_a_token_for_another_call_does_not_work_here(client, store):
    mine = exotel_call(store, "+919000001005", "EX-5")
    theirs = exotel_call(store, "+919000001006", "EX-6")

    response = client.post(f"/exotel/status/{mine}", params=signed(theirs),
                           data={"CallSid": "EX-5", "Status": "completed"})

    assert response.status_code == 403


def test_the_ip_allowlist_rejects_an_unknown_source_even_with_a_valid_token(client, store, monkeypatch):
    monkeypatch.setattr(exotel_routes, "EXOTEL_CALLBACK_ALLOWED_IPS", ("203.0.113.7",))
    call_id = exotel_call(store, "+919000001007", "EX-7")

    response = client.post(f"/exotel/status/{call_id}", params=signed(call_id),
                           data={"CallSid": "EX-7", "Status": "completed"})

    assert response.status_code == 403


def test_the_counter_is_shared_with_twilio_and_visible_under_both_names(client, store):
    """The key was renamed provider-neutral; the old one stays as an alias so
    existing dashboards and test_signature_alarm keep working."""
    call_id = exotel_call(store, "+919000001008", "EX-8")
    client.post(f"/exotel/status/{call_id}", data={"CallSid": "EX-8", "Status": "completed"})
    twilio_routes._record_signature_failure("status", "some-twilio-call")

    assert twilio_routes.callback_auth_failure_health()["total"] == 2
    assert twilio_routes.signature_failure_health() is not None
    assert twilio_routes.signature_failure_health()["total"] == 2
    assert twilio_routes.callback_auth_failure_health()["by_endpoint"] == {"exotel_status": 1, "status": 1}


def test_a_rejected_callback_never_logs_the_token(client, store, caplog):
    import logging

    call_id = exotel_call(store, "+919000001009", "EX-9")
    params = signed(call_id) | {"token": "a" * 64}
    with caplog.at_level(logging.WARNING):
        client.post(f"/exotel/status/{call_id}", params=params, data={"CallSid": "EX-9", "Status": "busy"})

    for record in caplog.records:
        assert "a" * 64 not in str(record.__dict__)


# ---------------------------------------------------------------------------
# Status vocabulary through the route
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "reported, lifecycle",
    [("completed", "COMPLETED"), ("failed", "FAILED"), ("busy", "BUSY"),
     ("no-answer", "NO_ANSWER"), ("canceled", "CANCELED")],
)
def test_terminal_exotel_statuses_terminalize_the_call(client, store, reported, lifecycle):
    call_id = exotel_call(store, f"+9190000020{ord(reported[0]) % 10}{len(reported)}", f"EX-T{reported}")

    client.post(f"/exotel/status/{call_id}", params=signed(call_id),
                data={"CallSid": f"EX-T{reported}", "Status": reported})

    saved = store.get_call(call_id)
    assert saved["provider_status"] == reported
    assert saved["lifecycle_state"] == lifecycle
    assert saved["provider_terminal_at"] is not None


def test_a_nonterminal_status_leaves_the_call_running(client, store):
    call_id = exotel_call(store, "+919000003001", "EX-N1")

    client.post(f"/exotel/status/{call_id}", params=signed(call_id),
                data={"CallSid": "EX-N1", "Status": "in-progress"})

    saved = store.get_call(call_id)
    assert saved["provider_status"] == "in-progress"
    assert saved["provider_terminal_at"] is None


def test_a_late_nonterminal_callback_cannot_regress_a_terminal_one(client, store):
    """Exotel redelivers callbacks and can deliver them out of order."""
    call_id = exotel_call(store, "+919000003002", "EX-N2")
    client.post(f"/exotel/status/{call_id}", params=signed(call_id),
                data={"CallSid": "EX-N2", "Status": "completed"})
    terminal_at = store.get_call(call_id)["provider_terminal_at"]

    client.post(f"/exotel/status/{call_id}", params=signed(call_id),
                data={"CallSid": "EX-N2", "Status": "in-progress"})

    saved = store.get_call(call_id)
    assert saved["provider_status"] == "completed"
    assert saved["provider_terminal_at"] == terminal_at
    assert saved["lifecycle_state"] == "COMPLETED"


def test_an_unknown_status_word_does_not_terminalize_anything(client, store):
    """Never invent an outcome from a word we do not recognise."""
    call_id = exotel_call(store, "+919000003003", "EX-N3")

    client.post(f"/exotel/status/{call_id}", params=signed(call_id),
                data={"CallSid": "EX-N3", "Status": "some-new-exotel-word"})

    assert store.get_call(call_id)["provider_terminal_at"] is None


def test_a_json_callback_body_is_accepted_too(client, store):
    """Exotel's callback content type is not guaranteed to be form-encoded."""
    call_id = exotel_call(store, "+919000003004", "EX-N4")

    response = client.post(f"/exotel/status/{call_id}", params=signed(call_id),
                           json={"CallSid": "EX-N4", "Status": "no-answer"})

    assert response.status_code == 200
    assert store.get_call(call_id)["provider_status"] == "no-answer"


def test_a_callback_naming_a_different_sid_does_not_touch_the_call(client, store):
    call_id = exotel_call(store, "+919000003005", "EX-N5")

    client.post(f"/exotel/status/{call_id}", params=signed(call_id),
                data={"CallSid": "EX-SOMEONE-ELSE", "Status": "completed"})

    assert store.get_call(call_id)["provider_terminal_at"] is None


# ---------------------------------------------------------------------------
# Media-stream correlation: token plus database, both required
# ---------------------------------------------------------------------------
def start_event(call_id: str, sid: str, *, token: str | None = None, ttl: int = 300) -> dict:
    expiry = int(time.time()) + ttl
    return {
        "event": "start",
        "sequence_number": 1,
        "stream_sid": "MZ1",
        "start": {
            "stream_sid": "MZ1",
            "call_sid": sid,
            "account_sid": "acme",
            "custom_parameters": {
                "call_id": call_id,
                "expiry": str(expiry),
                "token": exotel_stream_token(call_id, expiry) if token is None else token,
            },
            "media_format": {"encoding": "audio/x-raw", "sample_rate": "8000", "bit_rate": "16"},
        },
    }


def connect_media(client: TestClient, payload: dict) -> bool:
    """Returns whether the socket survived the correlation handshake."""
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect("/exotel/media-stream") as socket:
            socket.send_json(payload)
            try:
                socket.receive_json()
            except WebSocketDisconnect:
                return False
            return True
    except WebSocketDisconnect:
        return False


def test_a_correctly_correlated_stream_passes_the_handshake(client, store):
    """Positive control. Without this, every rejection test below could be
    passing because the helper always reports refusal.

    Correlation succeeding is observable in the database: `claim_media` takes
    ownership of the row. The call then goes on to need Deepgram, which this
    test does not provide, so only the handshake is asserted.
    """
    call_id = exotel_call(store, "+919000004000", "EX-M0")

    connect_media(client, start_event(call_id, "EX-M0"))

    claimed = store.get_call(call_id)
    assert claimed["media_owner"] is not None, "correlation should have claimed the media"
    assert claimed["media_connected"] == 1


def test_a_stream_whose_sid_does_not_match_the_bound_call_is_refused(client, store):
    """The token deliberately does not cover the CallSid -- it is minted
    before the dial, when no SID exists. The database supplies that half, and
    it has to actually be checked."""
    call_id = exotel_call(store, "+919000004001", "EX-M1")

    assert not connect_media(client, start_event(call_id, "EX-SOMEONE-ELSE"))


def test_a_stream_with_a_forged_token_is_refused_even_with_the_right_sid(client, store):
    call_id = exotel_call(store, "+919000004002", "EX-M2")

    assert not connect_media(client, start_event(call_id, "EX-M2", token="0" * 64))


def test_a_stream_for_a_twilio_call_is_refused(client, store):
    """Provider is persisted per call; the Exotel socket must not adopt a
    Twilio call even if everything else lines up."""
    call_id = store.enqueue_call(phone_number="+919000004003", provider="twilio")["call_id"]
    store.claim_job("owner", 10)
    store.bind_call_sid(call_id, "CA-M3", 45, 900)

    assert not connect_media(client, start_event(call_id, "CA-M3"))


def test_a_stream_for_a_terminal_call_is_refused(client, store):
    call_id = exotel_call(store, "+919000004004", "EX-M4")
    store.provider_status(call_id, "completed", "EX-M4")

    assert not connect_media(client, start_event(call_id, "EX-M4"))


def test_a_second_stream_cannot_take_over_an_owned_call(client, store):
    call_id = exotel_call(store, "+919000004005", "EX-M5")
    store.claim_media(call_id, "EX-M5", "first-owner")

    assert not connect_media(client, start_event(call_id, "EX-M5"))


def test_a_stream_with_no_start_event_is_refused(client, store):
    exotel_call(store, "+919000004006", "EX-M6")

    assert not connect_media(client, {"event": "connected"})


# ---------------------------------------------------------------------------
# Mounting
# ---------------------------------------------------------------------------
def test_the_exotel_routes_are_mounted_on_the_real_app():
    """A missing include_router would leave every other test in this file
    passing, because they mount the router themselves.

    Asserted by reachability rather than by inspecting `app.routes`: this
    FastAPI version keeps included routers opaque there, so a route-list check
    would silently prove nothing.
    """
    from fastapi.testclient import TestClient

    import app.main as main

    with TestClient(main.app, raise_server_exceptions=False) as probe:
        # Unauthenticated on purpose -- the operator middleware guards `/api/`
        # and `/twilio/outbound`, not carrier callbacks. 403 means the route
        # ran and rejected the missing token; 404 would mean it is not there.
        assert probe.post("/exotel/status/probe", data={}).status_code == 403
