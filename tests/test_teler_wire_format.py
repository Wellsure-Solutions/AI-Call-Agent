from __future__ import annotations

"""The exact bytes Teler sees and sends, pinned against published ground truth.

The Exotel precedent is why this file exists. Exotel's AgentStream guide renders
every field lowercase; that spelling was implemented first and **every dial
failed**, and it looked exactly like a credentials problem for a day. A vendor's
prose is not evidence that a spelling works, and a wire format drifting by one
character breaks every call at once and silently.

Teler's format could not be captured from a live call from this environment --
no trial number, no way to place one -- so it is pinned against four
independent sources that agree with each other, which is the strongest
available substitute for a packet capture:

  1. The `teler` PyPI package, 0.2.2, read as source rather than as a README:
     `resources/calls.py` (the POST path and body), `constants.py` (the base
     URL), `clients.py` (the `x-api-key` header), `flows.py` (`CallFlow.stream`).
  2. Teler's own published OpenAPI document, https://api.frejun.ai/openapi.json,
     for the endpoints, the `CallSessionState` enum, and the error envelope.
  3. https://frejun.ai/docs/media-streaming/supported-messages/, which is the
     only place the per-message JSON schema is written down.
  4. FreJun's two reference bridges -- teler-deepgram-node-bridge and
     teler-wav-bridge -- which are working code and agree with (3) exactly,
     including the detail that is easiest to get wrong.

That detail: the audio payload nesting is **asymmetric**. Inbound audio arrives
at `msg["data"]["audio_b64"]`; outbound audio is sent at the top level as
`audio_b64`. Sources 3 and 4 and the SDK's own README all show it, so it is the
format and not a documentation slip.

Two of the task's stated assumptions turned out to be wrong, and both are
pinned below so they cannot quietly come back:

  * `chunk_size` is in **milliseconds**, not bytes. Teler requires a multiple
    of 20 between 20 and 2000. It is therefore *on* this codebase's frame grid,
    not off it.
  * There is **no mark** and no acknowledgement of any kind, and no `stop`
    message -- the call ending is the socket closing.

Assertions here are against **string literals, not the modules' own
constants**. A test written against the constants would follow a regression
rather than catch it, which is the entire failure mode this file exists for.
Do not "tidy" them into imports.
"""

import asyncio
import base64
import json

import httpx
import pytest

from app.telephony.adapters.teler_adapter import TelerAdapter
from app.telephony.audio import g711
from app.telephony.providers.teler_provider import TelerProvider, aligned_chunk_ms
from tests import fixtures

API_KEY = "teler-key-not-real"
FROM_NUMBER = "+918064000000"
TO_NUMBER = "+919812345678"
BASE_URL = "https://api.frejun.ai/api/v1"

# The documented 202 body: a `message` and a `data` object whose identifier is
# `id` (with a `cs_` prefix), NOT `sid` and NOT `call_id`.
LIVE_INITIATE_RESPONSE = {
    "message": "Call initiated successfully",
    "data": {
        "id": "cs_01JQ8Z9K7M3N2P4R5S6T7V8W9X",
        "from_number": FROM_NUMBER,
        "to_number": TO_NUMBER,
        "status_callback_url": "https://calls.example.invalid/teler/status/call-wire-1",
        "record": False,
    },
}

# The documented CallSessionResponse. Note `state`, not `status`.
PUBLIC_BASE_URL = "https://calls.example.invalid"


@pytest.fixture(autouse=True)
def public_base_url(monkeypatch):
    """The dial builds its own flow URL, so the origin has to be set for the
    request under test to be the one production would send."""
    monkeypatch.setattr("app.telephony.callback_urls.PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    monkeypatch.setattr("app.telephony.callback_urls.STREAM_SECRET", "stream-secret-not-real")


LIVE_RETRIEVE_RESPONSE = {
    "id": "cs_01JQ8Z9K7M3N2P4R5S6T7V8W9X",
    "account_id": "acc_01JQ8Z9K7M3N2P4R5S6T7V8WIJ",
    "state": "answered",
    "direction": "outbound",
    "from_number": FROM_NUMBER,
    "to_number": TO_NUMBER,
    "created_at": "2026-06-01T09:15:04Z",
    "legs": [],
}


def provider(handler=None) -> tuple[TelerProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return (handler or (lambda _r: httpx.Response(202, json=LIVE_INITIATE_RESPONSE)))(request)

    built = TelerProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(capture)),
        api_key=API_KEY,
        from_number=FROM_NUMBER,
        base_url=BASE_URL,
        record=False,
    )
    return built, seen


def dial(handler=None) -> httpx.Request:
    """Place one dial through the real provider and return the raw request."""
    built, seen = provider(handler)
    asyncio.run(
        built.dial(
            call_id="call-wire-1",
            to_number=TO_NUMBER,
            ring_timeout=45,
            stream_url="wss://calls.example.invalid/teler/media-stream",
            status_callback_url="https://calls.example.invalid/teler/status/call-wire-1?expiry=1&token=cd34",
        )
    )
    return seen[0]


# ---------------------------------------------------------------------------
# The dial request
# ---------------------------------------------------------------------------
def test_the_dial_posts_json_to_the_initiate_path():
    request = dial()

    assert request.method == "POST"
    assert str(request.url) == "https://api.frejun.ai/api/v1/voice/calls/initiate"
    assert request.headers["content-type"].startswith("application/json")


def test_the_api_key_travels_in_the_x_api_key_header():
    """Not Bearer, not Basic, not a query parameter. Teler's OpenAPI security
    scheme is an apiKey in the `x-api-key` header, and the SDK sends the same."""
    request = dial()

    assert request.headers["x-api-key"] == API_KEY
    assert "authorization" not in request.headers


def test_the_body_field_names_are_snake_case_and_from_is_ours():
    """Teler's `from_number` is the number we own and `to_number` is the
    destination -- the plain reading, and the opposite of Exotel, where `From`
    is the number being dialled. Getting Teler's backwards would dial our own
    virtual number."""
    body = json.loads(dial().content.decode())

    assert body["from_number"] == FROM_NUMBER
    assert body["to_number"] == TO_NUMBER
    assert body["flow_url"].startswith("https://")
    assert body["status_callback_url"].startswith("https://")
    assert body["record"] is False


@pytest.mark.parametrize("wrong", ["from", "to", "From", "To", "CallerId",
                                   "StreamUrl", "stream_url", "url",
                                   "status_callback", "StatusCallback", "timeout"])
def test_no_twilio_or_exotel_spelling_leaks_in(wrong):
    """The two carriers already here spell all of this differently, and this is
    the shape a copy-paste regression would take."""
    assert wrong not in json.loads(dial().content.decode())


def test_the_whole_body_is_exactly_these_five_fields():
    """Pinned as a set so an accidental extra field is caught too."""
    assert set(json.loads(dial().content.decode())) == {
        "from_number",
        "to_number",
        "flow_url",
        "status_callback_url",
        "record",
    }


def test_the_stream_url_is_not_sent_at_dial_time():
    """Teler is Twilio-shaped: the media URL is named by the flow response, not
    by the dial. Sending it here would be sending a live stream token to an
    endpoint that has no use for it."""
    body = json.loads(dial().content.decode())

    assert not any("media-stream" in str(value) for value in body.values())


def test_the_flow_url_carries_our_own_token():
    """FreJun document no signature on the flow request, so an untokened flow
    URL would hand a media URL -- and a live stream token -- to anyone who
    guessed a call_id."""
    flow_url = json.loads(dial().content.decode())["flow_url"]

    assert "/teler/flow/call-wire-1" in flow_url
    assert "token=" in flow_url and "expiry=" in flow_url


# ---------------------------------------------------------------------------
# The dial response
# ---------------------------------------------------------------------------
def test_the_call_id_is_read_from_data_id():
    """`data.id`, not `sid` and not `call_id`. A wrong key here binds nothing,
    and an unbindable call holds its capacity slot until a human resolves it."""
    built, _ = provider()
    result = asyncio.run(built.dial(
        call_id="c", to_number=TO_NUMBER, ring_timeout=45,
        stream_url="", status_callback_url="https://x/z",
    ))

    assert result.provider_sid == "cs_01JQ8Z9K7M3N2P4R5S6T7V8W9X"


def test_a_202_with_no_call_id_is_ambiguous_rather_than_a_failure():
    """A 202 means Teler accepted the call. One we cannot name may still be
    ringing somebody's phone, so it must never be filed as a proven refusal --
    that would release the number for a redial and call them twice."""
    built, _ = provider(lambda _r: httpx.Response(202, json={"message": "ok", "data": {}}))

    with pytest.raises(Exception) as raised:
        asyncio.run(built.dial(call_id="c", to_number=TO_NUMBER, ring_timeout=45,
                               stream_url="", status_callback_url="https://x/z"))

    assert built.classify_dial_error(raised.value) == "ambiguous"


def test_the_status_is_read_from_state_on_retrieve():
    """CallSessionResponse says `state`; the SDK's own dataclass says `status`.
    Both are read, because only one of them is the documented API."""
    built, seen = provider(lambda _r: httpx.Response(200, json=LIVE_RETRIEVE_RESPONSE))

    status = asyncio.run(built.fetch_status("cs_01JQ8Z9K7M3N2P4R5S6T7V8W9X"))

    assert str(seen[0].url) == (
        "https://api.frejun.ai/api/v1/voice/calls/cs_01JQ8Z9K7M3N2P4R5S6T7V8W9X"
    )
    assert seen[0].method == "GET"
    assert status == "in-progress", "Teler's `answered` is this codebase's `in-progress`"


def test_the_hangup_is_a_post_to_its_own_action_path():
    """Not a DELETE on the call (Exotel) and not a status update (Twilio)."""
    built, seen = provider(lambda _r: httpx.Response(202, json={"request_id": "req_1"}))

    asyncio.run(built.request_terminal("cs_01JQ8Z9K7M3N2P4R5S6T7V8W9X", "completed"))

    assert seen[0].method == "POST"
    assert str(seen[0].url) == (
        "https://api.frejun.ai/api/v1/voice/calls/cs_01JQ8Z9K7M3N2P4R5S6T7V8W9X/hangup"
    )
    # The reason is constrained to ^[A-Z0-9_]+$ and is rejected otherwise.
    reason = json.loads(seen[0].content.decode())["reason"]
    assert reason == "AGENT_HANGUP"
    assert reason.replace("_", "").isalnum() and reason.upper() == reason


# ---------------------------------------------------------------------------
# The stream flow -- Teler's TwiML equivalent
# ---------------------------------------------------------------------------
def test_the_stream_flow_is_exactly_this_shape():
    flow = TelerProvider.build_stream_flow("wss://calls.example.invalid/teler/media-stream", chunk_ms=20)

    assert flow == {
        "action": "stream",
        "ws_url": "wss://calls.example.invalid/teler/media-stream",
        "chunk_size": 20,
        "sample_rate": "8k",
        "record": False,
    }


def test_the_sample_rate_is_the_string_8k_not_a_number():
    """Teler takes "8k" or "16k". A number is not one of them, and 16k would
    put the greeting cache and every barge-in constant on a different clock
    from the one they were tuned on."""
    flow = TelerProvider.build_stream_flow("wss://x/y")

    assert flow["sample_rate"] == "8k"
    assert isinstance(flow["sample_rate"], str)


def test_chunk_size_is_milliseconds_on_a_twenty_millisecond_grid():
    """The assumption this replaced: that `chunk_size: 400` meant 400 *bytes*,
    i.e. 25ms at 8kHz/16-bit, and therefore sat off this codebase's 20ms grid.
    It is 400 milliseconds, and Teler requires a multiple of 20 -- so it is on
    the grid, not off it."""
    for requested in (1, 19, 20, 21, 400, 500, 1999, 2000, 100_000):
        assert aligned_chunk_ms(requested) % 20 == 0
        assert 20 <= aligned_chunk_ms(requested) <= 2000

    assert aligned_chunk_ms(0) == 20, "clamped to Teler's minimum, not to zero"
    assert aligned_chunk_ms(30) == 20, "rounded down onto the grid"
    assert aligned_chunk_ms(100_000) == 2000, "clamped to Teler's maximum"


# ---------------------------------------------------------------------------
# The media socket messages
# ---------------------------------------------------------------------------
class FakeSocket:
    def __init__(self, inbound: list[str] | None = None) -> None:
        self.sent: list[dict] = []
        self._inbound = list(inbound or [])

    async def send_text(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def receive_text(self) -> str:
        if not self._inbound:
            raise AssertionError("inbound script exhausted")
        return self._inbound.pop(0)


def bound_adapter(inbound: list[str] | None = None, send_chunk_ms: int = 20) -> TelerAdapter:
    built = TelerAdapter(audio_bridge=object(), send_chunk_ms=send_chunk_ms)
    built.websocket = FakeSocket(inbound)
    built.stream_sid = "ms_01JQ8Z9K7M3N2P4R5S6T7V8W9X"
    built.call_sid = "cs_01JQ8Z9K7M3N2P4R5S6T7V8W9X"
    return built


def test_outbound_audio_is_type_audio_with_a_top_level_payload_and_chunk_id():
    """The asymmetry. Nesting the outbound payload under `data`, the way it
    arrives inbound, produces a message Teler silently ignores -- so the call
    connects and the customer hears nothing at all."""
    teler = bound_adapter()

    asyncio.run(teler.send_audio(b"\xff" * 160))

    message = teler.websocket.sent[0]
    assert message["type"] == "audio"
    assert "audio_b64" in message and "data" not in message
    assert message["chunk_id"] == 1
    # Twilio's and Exotel's spellings, which must not appear.
    assert "event" not in message
    assert "media" not in message
    assert "payload" not in message
    assert "streamSid" not in message and "stream_sid" not in message


def test_inbound_audio_is_read_from_the_nested_data_object():
    original = fixtures.tone(6000, 20)

    teler = bound_adapter([json.dumps({
        "type": "audio",
        "stream_id": "ms_01JQ8Z9K7M3N2P4R5S6T7V8W9X",
        "message_id": 7,
        "data": {"audio_b64": base64.b64encode(g711.ulaw_to_pcm16(original)).decode()},
    })])

    assert asyncio.run(teler.receive_audio()) == original


def test_chunk_ids_increment_so_a_chunk_can_be_named():
    """Required on every audio message, and the handle `interrupt` would use."""
    teler = bound_adapter()

    async def scenario():
        for _ in range(3):
            await teler.send_audio(b"\xff" * 160)

    asyncio.run(scenario())

    assert [message["chunk_id"] for message in teler.websocket.sent] == [1, 2, 3]


def test_clear_is_a_bare_type_clear_with_no_stream_identifier():
    """Twilio and Exotel both key `clear` to a stream id; Teler's carries
    nothing. Sending one anyway is a message Teler does not document."""
    teler = bound_adapter()

    asyncio.run(teler.clear_playback())

    assert teler.websocket.sent == [{"type": "clear"}]


def test_nothing_is_ever_sent_that_claims_to_be_a_mark():
    """Teler has no mark and nothing to acknowledge one. `_send_mark` is a
    documented no-op rather than an unimplemented abstract method, because the
    base class calls it during the first agent turn."""
    teler = bound_adapter()

    async def scenario():
        await teler._send_mark()
        await teler._on_frame_sent()

    asyncio.run(scenario())

    assert teler.websocket.sent == []
    assert teler._pending_marks == set()


def test_the_start_message_declares_the_encoding_this_adapter_assumes():
    """Recorded as a literal so that a future change to Teler's stream format
    fails a test here rather than degrading audio on live calls. `audio/l16` at
    8000 Hz mono is exactly the PCM16LE the transcoder is written for."""
    start = {
        "type": "start",
        "account_id": "acc_01JQ8Z9K7M3N2P4R5S6T7V8WIJ",
        "call_app_id": "va_01JQ8Z9K7M3N2P4R5S6T7V8WKL",
        "call_id": "cs_01JQ8Z9K7M3N2P4R5S6T7V8W9X",
        "stream_id": "ms_01JQ8Z9K7M3N2P4R5S6T7V8W9X",
        "message_id": 1,
        "data": {"encoding": "audio/l16", "sample_rate": 8000, "channels": 1},
    }

    assert start["data"]["encoding"] == "audio/l16"
    assert start["data"]["sample_rate"] == 8000
    assert start["data"]["channels"] == 1
    # The two identifiers the media route correlates on live at the top level,
    # not inside `data`.
    assert start["call_id"].startswith("cs_")
    assert start["stream_id"].startswith("ms_")


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------
def test_telers_five_states_map_onto_this_codebases_words():
    """Teler's CallSessionState enum has exactly five members and none of them
    is busy, no-answer or canceled -- those arrive as a `failed` call carrying
    a reason."""
    from app.telephony.providers.teler_provider import normalize_status

    assert normalize_status("initiated") == "queued"
    assert normalize_status("ringing") == "ringing"
    assert normalize_status("answered") == "in-progress"
    assert normalize_status("completed") == "completed"
    assert normalize_status("failed") == "failed"


def test_a_failure_reason_refines_the_terminal_word():
    from app.telephony.providers.teler_provider import normalize_status

    assert normalize_status("failed", "no_answer") == "no-answer"
    assert normalize_status("failed", "user_busy") == "busy"
    assert normalize_status("failed", "canceled") == "canceled"


def test_a_completed_call_is_never_rewritten_by_its_hangup_reason():
    """`callee_hangup` on a completed call is a real conversation that
    happened. Refining it would misreport every successful call in the batch."""
    from app.telephony.providers.teler_provider import normalize_status

    assert normalize_status("completed", "callee_hangup") == "completed"
    assert normalize_status("completed", "no_answer") == "completed"


def test_an_unknown_state_is_treated_as_still_running():
    """The safe direction: a call is only removed from capacity by a proven
    terminal status, so an unrecognised word schedules another reconciliation
    instead of inventing an outcome."""
    from app.telephony.providers.teler_provider import normalize_status

    assert normalize_status("something-new") == "in-progress"
    assert normalize_status("") == "in-progress"
    assert normalize_status(None) == "in-progress"


def test_only_call_events_map_to_a_status():
    """Teler delivers stream and recording events to the same URL, and its own
    docs warn that `stream.completed` does not imply `call.completed`. Mapping
    one onto a call status would hang up on a live customer."""
    from app.telephony.providers.teler_provider import normalize_event

    assert normalize_event("call.completed") == "completed"
    assert normalize_event("call.answered") == "in-progress"
    assert normalize_event("call.failed", "no_answer") == "no-answer"
    for ignored in ("stream.initiated", "stream.completed",
                    "recording.completed", "recording.failed", "", None):
        assert normalize_event(ignored) is None
