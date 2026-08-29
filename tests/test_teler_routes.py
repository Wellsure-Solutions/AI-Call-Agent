from __future__ import annotations

"""Teler's flow endpoint, webhooks, and media socket.

Three endpoints, three different things being proved:

  * `/teler/flow/{call_id}` is where the media URL is minted, so an
    unauthenticated one hands a live stream token to anyone who guesses a
    call_id. It is also where Teler's own call id is bound, which is what every
    later correlation check compares against.

  * `/teler/status/{call_id}` must map only *call* events onto a status. Teler
    delivers stream and recording events to the same URL and its own docs warn
    that `stream.completed` does not imply `call.completed`.

  * `/teler/media-stream` correlates in two halves -- token and database --
    exactly like Exotel's. The token was originally checked against the id in
    the start message, on the reasoning that the flow route already knew one;
    a real call proved Teler names the call differently on that socket than
    anywhere else, so it is checked against the call row instead.
"""

import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.storage.sqlite_store import SQLiteCallStore
from app.telephony import teler_routes, twilio_routes
from app.telephony.callback_urls import (
    teler_callback_token,
    teler_flow_token,
    teler_stream_token,
)

SECRET = "test-secret-not-real"
PUBLIC_BASE_URL = "https://calls.example.invalid"


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setattr("app.telephony.callback_urls.STREAM_SECRET", SECRET)
    monkeypatch.setattr("app.telephony.callback_urls.PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    monkeypatch.setattr(teler_routes, "TELER_WEBHOOK_SECRET", "")
    twilio_routes._signature_failures.update(total=0, by_endpoint={}, last_at=None, last_endpoint=None)
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


def teler_call(store: SQLiteCallStore, phone: str, sid: str | None = None) -> str:
    """A queued Teler call, optionally already bound to a carrier id."""
    call_id = store.enqueue_call(phone_number=phone, provider="teler")["call_id"]
    store.claim_job("owner", 10)
    if sid:
        store.bind_call_sid(call_id, sid, 45, 900)
    return call_id


def flow_params(call_id: str, ttl: int = 3600) -> dict[str, str]:
    expiry = int(time.time()) + ttl
    return {"expiry": str(expiry), "token": teler_flow_token(call_id, expiry)}


def status_params(call_id: str, ttl: int = 3600) -> dict[str, str]:
    expiry = int(time.time()) + ttl
    return {"expiry": str(expiry), "token": teler_callback_token(call_id, expiry)}


def flow_body(teler_call_id: str) -> dict:
    """What Teler POSTs to a flow URL."""
    return {
        "call_id": teler_call_id,
        "account_id": "acc_01JQ8Z9K7M3N2P4R5S6T7V8WIJ",
        "from_number": "+918064000000",
        "to_number": "+919812345678",
        "direction": "outbound",
    }


# ---------------------------------------------------------------------------
# The flow endpoint
# ---------------------------------------------------------------------------
def test_a_correctly_signed_flow_request_returns_a_stream_action(client, store):
    call_id = teler_call(store, "+919000001001", "cs_1")

    response = client.post(f"/teler/flow/{call_id}", params=flow_params(call_id),
                           json=flow_body("cs_1"))

    assert response.status_code == 200
    flow = response.json()
    assert flow["action"] == "stream"
    assert flow["ws_url"].startswith("wss://")
    assert flow["chunk_size"] % 20 == 0
    assert flow["sample_rate"] == "8k"


def test_the_flow_response_carries_a_token_covering_telers_call_id(client, store):
    """The whole reason the media URL is minted here and not at dial time."""
    call_id = teler_call(store, "+919000001002", "cs_2")

    ws_url = client.post(f"/teler/flow/{call_id}", params=flow_params(call_id),
                         json=flow_body("cs_2")).json()["ws_url"]

    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(ws_url).query)
    assert query["call_id"] == [call_id]
    expiry = int(query["expiry"][0])
    assert query["token"] == [teler_stream_token(call_id, "cs_2", expiry)]
    # A token minted for a different carrier id must not validate.
    assert query["token"] != [teler_stream_token(call_id, "cs_OTHER", expiry)]


def test_a_flow_request_with_a_bad_token_is_rejected_and_counted(client, store):
    call_id = teler_call(store, "+919000001003", "cs_3")
    params = flow_params(call_id) | {"token": "0" * 64}

    response = client.post(f"/teler/flow/{call_id}", params=params, json=flow_body("cs_3"))

    assert response.status_code == 403
    assert twilio_routes.callback_auth_failure_health()["by_endpoint"] == {"teler_flow": 1}


def test_a_flow_request_with_no_token_at_all_is_rejected(client, store):
    call_id = teler_call(store, "+919000001004", "cs_4")

    assert client.post(f"/teler/flow/{call_id}", json=flow_body("cs_4")).status_code == 403


def test_an_expired_flow_token_is_rejected(client, store):
    call_id = teler_call(store, "+919000001005", "cs_5")
    expiry = int(time.time()) - 1
    params = {"expiry": str(expiry), "token": teler_flow_token(call_id, expiry)}

    assert client.post(f"/teler/flow/{call_id}", params=params,
                       json=flow_body("cs_5")).status_code == 403


def test_a_flow_token_for_another_call_does_not_work_here(client, store):
    mine = teler_call(store, "+919000001006", "cs_6")
    theirs = teler_call(store, "+919000001007", "cs_7")

    response = client.post(f"/teler/flow/{mine}", params=flow_params(theirs),
                           json=flow_body("cs_6"))

    assert response.status_code == 403


def test_a_flow_request_naming_a_different_call_id_fails_correlation(client, store):
    """The SID is re-bound here, and `bind_call_sid` refuses a row already
    bound to something else. Without that check this would mint a stream token
    for a call id that is not this call's."""
    call_id = teler_call(store, "+919000001008", "cs_8")

    response = client.post(f"/teler/flow/{call_id}", params=flow_params(call_id),
                           json=flow_body("cs_SOMEONE_ELSE"))

    assert response.status_code == 409
    assert store.get_call(call_id)["call_sid"] == "cs_8"


def test_a_flow_request_for_an_unbound_call_binds_it(client, store):
    """Teler fetches the flow when the call connects, which is normally after
    the coordinator has bound the id the dial returned. If it ever arrives
    first, binding here is what keeps the call correlatable."""
    call_id = teler_call(store, "+919000001009")
    assert store.get_call(call_id)["call_sid"] is None

    response = client.post(f"/teler/flow/{call_id}", params=flow_params(call_id),
                           json=flow_body("cs_9"))

    assert response.status_code == 200
    assert store.get_call(call_id)["call_sid"] == "cs_9"


def test_a_flow_request_for_a_call_on_another_provider_is_refused(client, store):
    """Provider is persisted per call. A Twilio call must never be handed a
    Teler media URL, whatever else lines up."""
    call_id = store.enqueue_call(phone_number="+919000001010", provider="twilio")["call_id"]
    store.claim_job("owner", 10)

    response = client.post(f"/teler/flow/{call_id}", params=flow_params(call_id),
                           json=flow_body("cs_10"))

    assert response.status_code == 409


def test_a_flow_request_for_an_unknown_call_is_refused(client, store):
    assert client.post("/teler/flow/no-such-call", params=flow_params("no-such-call"),
                       json=flow_body("cs_11")).status_code == 409


# ---------------------------------------------------------------------------
# The status webhook
# ---------------------------------------------------------------------------
def envelope_2026(event: str, sid: str, **data) -> dict:
    return {
        "id": "evt_01H8ZXK9M2P7Q3R4S5T6V7W8XY",
        "type": event,
        "api_version": "2026-06-01",
        "occurred_at": "2026-06-01T11:32:18.501Z",
        "account_id": "acc_01H8ZXK9M2P7Q3R4S5T6V7W8XY",
        "voice_app_id": "va_01H8ZXK9M2P7Q3R4S5T6V7W8XY",
        "sip_trunk_id": None,
        "call_id": sid,
        "leg_id": None,
        "data": {"call_id": sid, **data},
        "previous_attributes": None,
    }


def flat_2025(event: str, sid: str, **data) -> dict:
    return {
        "event": event,
        "account_id": "1f2e3d4c-0000-0000-0000-000000000000",
        "call_app_id": "9a8b7c6d-0000-0000-0000-000000000000",
        "data": {"call_id": sid, **data},
    }


def test_a_correctly_signed_status_callback_is_accepted(client, store):
    call_id = teler_call(store, "+919000002001", "cs_S1")

    response = client.post(f"/teler/status/{call_id}", params=status_params(call_id),
                           json=envelope_2026("call.completed", "cs_S1", reason="callee_hangup"))

    assert response.status_code == 200
    assert store.get_call(call_id)["provider_status"] == "completed"


def test_the_older_flat_payload_version_is_understood_too(client, store):
    """The version is pinned per Voice App in FreJun's dashboard and can be
    changed there without a deploy, so both shapes have to work."""
    call_id = teler_call(store, "+919000002002", "cs_S2")

    response = client.post(f"/teler/status/{call_id}", params=status_params(call_id),
                           json=flat_2025("call.completed", "cs_S2"))

    assert response.status_code == 200
    assert store.get_call(call_id)["provider_status"] == "completed"


@pytest.mark.parametrize(
    "event, reason, expected, lifecycle",
    [
        ("call.completed", "callee_hangup", "completed", "COMPLETED"),
        ("call.failed", "no_answer", "no-answer", "NO_ANSWER"),
        ("call.failed", "user_busy", "busy", "BUSY"),
        ("call.failed", "canceled", "canceled", "CANCELED"),
        ("call.failed", "carrier_error", "failed", "FAILED"),
    ],
)
def test_terminal_events_terminalize_the_call(client, store, event, reason, expected, lifecycle):
    sid = f"cs_T{reason}"
    call_id = teler_call(store, f"+9190000210{len(reason) % 10}", sid)

    client.post(f"/teler/status/{call_id}", params=status_params(call_id),
                json=envelope_2026(event, sid, reason=reason))

    saved = store.get_call(call_id)
    assert saved["provider_status"] == expected
    assert saved["lifecycle_state"] == lifecycle
    assert saved["provider_terminal_at"] is not None


def test_a_nonterminal_event_leaves_the_call_running(client, store):
    call_id = teler_call(store, "+919000002003", "cs_S3")

    client.post(f"/teler/status/{call_id}", params=status_params(call_id),
                json=envelope_2026("call.answered", "cs_S3"))

    saved = store.get_call(call_id)
    assert saved["provider_status"] == "in-progress"
    assert saved["provider_terminal_at"] is None


@pytest.mark.parametrize("event", ["stream.initiated", "stream.completed",
                                   "recording.completed", "recording.failed"])
def test_stream_and_recording_events_never_touch_the_call_status(client, store, event):
    """FreJun's own warning: a stream can end while the call is still up.
    Mapping `stream.completed` onto `call.completed` would hang up on a live
    customer and release their number for a redial."""
    call_id = teler_call(store, "+919000002004", "cs_S4")
    client.post(f"/teler/status/{call_id}", params=status_params(call_id),
                json=envelope_2026("call.answered", "cs_S4"))

    response = client.post(f"/teler/status/{call_id}", params=status_params(call_id),
                           json=envelope_2026(event, "cs_S4"))

    assert response.status_code == 200
    saved = store.get_call(call_id)
    assert saved["provider_status"] == "in-progress"
    assert saved["provider_terminal_at"] is None


def test_an_unknown_event_name_does_not_terminalize_anything(client, store):
    call_id = teler_call(store, "+919000002005", "cs_S5")

    client.post(f"/teler/status/{call_id}", params=status_params(call_id),
                json=envelope_2026("call.something_new", "cs_S5"))

    assert store.get_call(call_id)["provider_terminal_at"] is None


def test_a_late_nonterminal_callback_cannot_regress_a_terminal_one(client, store):
    """Teler retries up to eight times and orders only best-effort per call."""
    call_id = teler_call(store, "+919000002006", "cs_S6")
    client.post(f"/teler/status/{call_id}", params=status_params(call_id),
                json=envelope_2026("call.completed", "cs_S6"))
    terminal_at = store.get_call(call_id)["provider_terminal_at"]

    client.post(f"/teler/status/{call_id}", params=status_params(call_id),
                json=envelope_2026("call.answered", "cs_S6"))

    saved = store.get_call(call_id)
    assert saved["provider_status"] == "completed"
    assert saved["provider_terminal_at"] == terminal_at


def test_a_callback_naming_a_different_call_id_does_not_touch_the_call(client, store):
    call_id = teler_call(store, "+919000002007", "cs_S7")

    client.post(f"/teler/status/{call_id}", params=status_params(call_id),
                json=envelope_2026("call.completed", "cs_SOMEONE_ELSE"))

    assert store.get_call(call_id)["provider_terminal_at"] is None


def test_a_callback_with_a_bad_token_is_rejected_and_counted(client, store):
    call_id = teler_call(store, "+919000002008", "cs_S8")
    params = status_params(call_id) | {"token": "0" * 64}

    response = client.post(f"/teler/status/{call_id}", params=params,
                           json=envelope_2026("call.completed", "cs_S8"))

    assert response.status_code == 403
    assert store.get_call(call_id)["provider_terminal_at"] is None
    assert twilio_routes.callback_auth_failure_health()["by_endpoint"] == {"teler_status": 1}


def test_a_malformed_body_is_acknowledged_rather_than_five_hundred(client, store):
    """A 5xx makes Teler redeliver up to eight times, and none of the retries
    helps the call the body refers to."""
    call_id = teler_call(store, "+919000002009", "cs_S9")

    response = client.post(f"/teler/status/{call_id}", params=status_params(call_id),
                           content=b"not json at all",
                           headers={"content-type": "application/json"})

    assert response.status_code == 200


def test_a_rejected_callback_never_logs_the_token(client, store, caplog):
    import logging

    call_id = teler_call(store, "+919000002010", "cs_S10")
    params = status_params(call_id) | {"token": "a" * 64}
    with caplog.at_level(logging.WARNING):
        client.post(f"/teler/status/{call_id}", params=params,
                    json=envelope_2026("call.completed", "cs_S10"))

    for record in caplog.records:
        assert "a" * 64 not in str(record.__dict__)


# ---------------------------------------------------------------------------
# Teler's own signature, when we have the secret to check it with
# ---------------------------------------------------------------------------
def signed_headers(body: bytes, secret: str, encoding: str = "hex") -> dict[str, str]:
    timestamp = str(int(time.time()))
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    import base64 as _b64

    value = digest.hexdigest() if encoding == "hex" else _b64.b64encode(digest.digest()).decode()
    return {
        "x-teler-timestamp": timestamp,
        "x-teler-signature": value,
        "content-type": "application/json",
    }


@pytest.mark.parametrize("encoding", ["hex", "base64"])
def test_a_validly_signed_callback_is_accepted_in_either_digest_encoding(client, store, monkeypatch, encoding):
    """FreJun publish the algorithm and the signed string but not the digest
    encoding, so both are accepted. Guessing one and being wrong would reject
    every callback -- a total, silent outage."""
    monkeypatch.setattr(teler_routes, "TELER_WEBHOOK_SECRET", "webhook-secret-not-real")
    call_id = teler_call(store, "+919000005001", "cs_G1")
    body = json.dumps(envelope_2026("call.completed", "cs_G1")).encode()

    response = client.post(f"/teler/status/{call_id}", params=status_params(call_id),
                           content=body,
                           headers=signed_headers(body, "webhook-secret-not-real", encoding))

    assert response.status_code == 200
    assert store.get_call(call_id)["provider_status"] == "completed"


def test_a_wrongly_signed_callback_is_rejected_even_with_a_valid_token(client, store, monkeypatch):
    monkeypatch.setattr(teler_routes, "TELER_WEBHOOK_SECRET", "webhook-secret-not-real")
    call_id = teler_call(store, "+919000005002", "cs_G2")
    body = json.dumps(envelope_2026("call.completed", "cs_G2")).encode()

    response = client.post(f"/teler/status/{call_id}", params=status_params(call_id),
                           content=body,
                           headers=signed_headers(body, "a-different-secret"))

    assert response.status_code == 403
    assert store.get_call(call_id)["provider_terminal_at"] is None


def test_our_own_token_is_still_required_when_the_signature_is_valid(client, store, monkeypatch):
    """The signature is defence in depth, never a replacement. A dashboard
    secret we cannot confirm is set must not become the only control."""
    monkeypatch.setattr(teler_routes, "TELER_WEBHOOK_SECRET", "webhook-secret-not-real")
    call_id = teler_call(store, "+919000005003", "cs_G3")
    body = json.dumps(envelope_2026("call.completed", "cs_G3")).encode()

    response = client.post(f"/teler/status/{call_id}",
                           params=status_params(call_id) | {"token": "0" * 64},
                           content=body,
                           headers=signed_headers(body, "webhook-secret-not-real"))

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Media-stream correlation: token plus database, both required
# ---------------------------------------------------------------------------
# Teler names the same call differently on the media socket than on its REST
# and webhook surfaces. Both of these came off one real call:
#
#   REST / flow / webhooks : 1b3faa9c-4383-4d87-9571-bd51751950cb
#   media socket start     : cs_5NGFF35W81ACMAH2PEVCQVCQ93
#
# The fixtures below keep them deliberately unrelated, because a fixture that
# used one value for both would pass whether or not the route still compared
# them -- which is the bug that took a live call to find.
REST_ID = "1b3faa9c-4383-4d87-9571-bd51751950cb"
MEDIA_ID = "cs_5NGFF35W81ACMAH2PEVCQVCQ93"


def start_message(media_call_id: str = MEDIA_ID) -> dict:
    """A real Teler start message, verbatim apart from the ids."""
    return {
        "type": "start",
        "account_id": "acc_21VB4C0NP18X3RCX8EVA0B0F1M",
        "call_app_id": "va_2YYAYTG2F698FA76FNM12Z0RZ4",
        "call_id": media_call_id,
        "stream_id": "ms_0VCX9M023M8GYA4RJARK06QJPF",
        "message_id": 1,
        "data": {"encoding": "audio/l16", "sample_rate": 8000, "channels": 1},
    }


def _media_query(call_id: str, sid: str, token: str | None, ttl: int) -> dict[str, str]:
    expiry = int(time.time()) + ttl
    return {
        "call_id": call_id,
        "expiry": str(expiry),
        "token": teler_stream_token(call_id, sid, expiry) if token is None else token,
    }


def connect_media(client: TestClient, call_id: str, sid: str,
                  token: str | None = None, ttl: int = 300,
                  media_call_id: str = MEDIA_ID) -> bool:
    """Returns whether the socket survived the correlation handshake.

    Only meaningful for the refusal cases: a stream that correlates keeps the
    socket open for the life of the call and sends nothing, so waiting for a
    message would block forever by design. Use `open_media` for those.

    `sid` is the id on the call row -- what the flow route bound and what the
    token is minted over. `media_call_id` is the unrelated id the start
    message announces.
    """
    from starlette.websockets import WebSocketDisconnect

    query = _media_query(call_id, sid, token, ttl)
    try:
        with client.websocket_connect("/teler/media-stream", params=query) as socket:
            socket.send_json(start_message(media_call_id))
            try:
                socket.receive_json()
            except WebSocketDisconnect:
                return False
            return True
    except WebSocketDisconnect:
        return False


def open_media(client: TestClient, call_id: str, sid: str, settled,
               token: str | None = None, ttl: int = 300,
               media_call_id: str = MEDIA_ID) -> None:
    """Open the socket, send start, and wait for the handler to get going.

    The success path is asserted against the database and the spy rather than
    against anything on the wire, because a correlated stream produces nothing
    on the wire until the agent speaks -- so there is no message to wait on,
    and leaving the socket open forever is what production does.

    `settled` is polled instead: the app runs on its own thread, so closing the
    socket the instant after `send_json` races the handler and the assertions
    see nothing. Bounded so a genuine failure fails the test rather than
    hanging it.
    """
    from starlette.websockets import WebSocketDisconnect

    query = _media_query(call_id, sid, token, ttl)
    try:
        with client.websocket_connect("/teler/media-stream", params=query) as socket:
            socket.send_json(start_message(media_call_id))
            deadline = time.time() + 2.0
            while time.time() < deadline and not settled():
                time.sleep(0.01)
    except WebSocketDisconnect:
        pass


@pytest.fixture()
def spy_adapter(monkeypatch) -> dict:
    """Stand in for the media plane on the paths where correlation succeeds.

    Both stubs are needed and neither is under test here. The real adapter
    blocks on the socket for the life of the call, and the real AudioBridge's
    `stop()` drives the conversation engine, which reaches for Deepgram. A
    test that gets past correlation hangs on both -- which is itself proof the
    correlation fix works, but not a usable test.
    """
    captured: dict = {}

    class SpyBridge:
        def __init__(self, session, *_args, **_kwargs):
            self.session = session

        async def stop(self, status: str = "completed") -> None:
            captured["bridge_stop"] = status

    class SpyAdapter:
        def __init__(self, *_args, **_kwargs):
            self.pending_greeting = None
            self.call_sid = None
            self.stream_sid = None
            self.websocket = None

        def attach(self, session):
            captured["session"] = session

        async def start(self):
            captured["call_sid"] = self.call_sid
            captured["stream_sid"] = self.stream_sid

    monkeypatch.setattr(teler_routes, "AudioBridge", SpyBridge)
    monkeypatch.setattr(teler_routes, "TelerAdapter", SpyAdapter)
    return captured


def test_a_stream_correlates_even_though_the_start_message_names_the_call_differently(
    client, store, spy_adapter
):
    """The regression that broke every live call.

    The route used to take the carrier id from the start message and validate
    the token against it. Teler announces a `cs_`-prefixed id there and a raw
    UUID everywhere else, so the token never matched, the socket closed 1008,
    and the customer heard silence until Teler hung up.
    """
    call_id = teler_call(store, "+919000004000", REST_ID)

    open_media(client, call_id, REST_ID, settled=lambda: "call_sid" in spy_adapter)

    claimed = store.get_call(call_id)
    assert claimed["media_owner"] is not None, "correlation should have claimed the media"
    assert claimed["media_connected"] == 1
    assert claimed["call_sid"] == REST_ID, "the media id must never overwrite the REST one"


def test_the_adapter_is_given_the_rest_id_not_the_media_id(client, store, spy_adapter):
    """The hangup is a REST call, so it takes the REST id. Handing the adapter
    the media-plane id would 404 every agent-initiated hangup and leave the
    call running to its maximum-duration deadline."""
    call_id = teler_call(store, "+919000004009", REST_ID)

    open_media(client, call_id, REST_ID, settled=lambda: "call_sid" in spy_adapter)

    assert spy_adapter["call_sid"] == REST_ID
    assert spy_adapter["call_sid"] != MEDIA_ID
    # The stream handle is the media plane's own, which is what it is for.
    assert spy_adapter["stream_sid"] == "ms_0VCX9M023M8GYA4RJARK06QJPF"


def test_both_identifiers_are_recorded_on_the_session(client, store, spy_adapter):
    """The media id appears nowhere else, so it has to be kept here to be
    quotable when raising a stream problem with FreJun."""
    call_id = teler_call(store, "+919000004013", REST_ID)

    open_media(client, call_id, REST_ID, settled=lambda: "session" in spy_adapter)

    metadata = spy_adapter["session"].metadata
    assert metadata["call_sid"] == REST_ID
    assert metadata["teler_media_call_id"] == MEDIA_ID


def test_a_stream_with_a_forged_token_is_refused(client, store):
    call_id = teler_call(store, "+919000004002", "cs_M2")

    assert not connect_media(client, call_id, "cs_M2", token="0" * 64)
    assert store.get_call(call_id)["media_owner"] is None


def test_a_token_minted_for_a_different_call_does_not_validate(client, store):
    """A replayed URL from another call must not bind this one's audio."""
    mine = teler_call(store, "+919000004003", "cs_M3")
    theirs = teler_call(store, "+919000004010", "cs_M3b")
    expiry = int(time.time()) + 300

    assert not connect_media(client, mine, "cs_M3",
                             token=teler_stream_token(theirs, "cs_M3b", expiry))
    assert store.get_call(mine)["media_owner"] is None


def test_a_token_minted_for_a_different_bound_id_does_not_validate(client, store):
    """The token still covers the id on the call row, so a token minted before
    a re-bind cannot be used after it."""
    call_id = teler_call(store, "+919000004011", "cs_M3c")
    expiry = int(time.time()) + 300
    wrong = teler_stream_token(call_id, "cs_SOMETHING_ELSE", expiry)

    assert not connect_media(client, call_id, "cs_M3c", token=wrong)


def test_an_expired_stream_token_is_refused(client, store):
    call_id = teler_call(store, "+919000004004", "cs_M4")

    assert not connect_media(client, call_id, "cs_M4", ttl=-1)


def test_a_stream_for_a_call_with_no_bound_id_is_refused(client, store):
    """The flow route binds the id. Without it there is nothing to validate the
    token against, so the stream cannot be trusted."""
    call_id = store.enqueue_call(phone_number="+919000004012", provider="teler")["call_id"]
    store.claim_job("owner", 10)

    assert not connect_media(client, call_id, "cs_M9")
    assert store.get_call(call_id)["media_owner"] is None


def test_a_stream_for_an_unknown_call_is_refused(client, store):
    assert not connect_media(client, "no-such-call", "cs_M10")


def test_a_stream_for_a_call_on_another_provider_is_refused(client, store):
    call_id = store.enqueue_call(phone_number="+919000004005", provider="twilio")["call_id"]
    store.claim_job("owner", 10)
    store.bind_call_sid(call_id, "cs_M5", 45, 900)

    assert not connect_media(client, call_id, "cs_M5")


def test_a_stream_for_a_terminal_call_is_refused(client, store):
    call_id = teler_call(store, "+919000004006", "cs_M6")
    store.provider_status(call_id, "completed", "cs_M6")

    assert not connect_media(client, call_id, "cs_M6")


def test_a_second_stream_cannot_take_over_an_owned_call(client, store):
    call_id = teler_call(store, "+919000004007", "cs_M7")
    store.claim_media(call_id, "cs_M7", "first-owner")

    assert not connect_media(client, call_id, "cs_M7")


def test_a_stream_with_no_start_message_is_refused(client, store):
    from starlette.websockets import WebSocketDisconnect

    call_id = teler_call(store, "+919000004008", "cs_M8")
    expiry = int(time.time()) + 300
    query = {"call_id": call_id, "expiry": str(expiry),
             "token": teler_stream_token(call_id, "cs_M8", expiry)}

    survived = True
    try:
        with client.websocket_connect("/teler/media-stream", params=query) as socket:
            socket.send_json({"type": "audio", "data": {"audio_b64": ""}})
            socket.send_json({"type": "audio", "data": {"audio_b64": ""}})
            socket.send_json({"type": "audio", "data": {"audio_b64": ""}})
            try:
                socket.receive_json()
            except WebSocketDisconnect:
                survived = False
    except WebSocketDisconnect:
        survived = False

    assert not survived
    assert store.get_call(call_id)["media_owner"] is None
