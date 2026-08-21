from __future__ import annotations

"""Exotel's media plane: framing, tokens, callback auth, and the clock.

The framing tests exist because Exotel's wire format differs from Twilio's in
three ways at once (encoding, chunk size, message key names) while everything
above the socket must not be able to tell. The clock test exists because the
re-framer buffers, and a buffered timestamp would make the headline latency
metric read low on Exotel only -- so the two carriers would stop being
comparable on the same conversation.
"""

import asyncio
import base64
import json
import time

import pytest

from app.telephony.adapters.exotel_adapter import (
    MULAW_FRAME_BYTES,
    PCM_FRAME_BYTES,
    ExotelAdapter,
    _aligned_chunk_bytes,
)
from app.telephony.adapters.twilio_adapter import TwilioAdapter
from app.telephony.audio import g711
from app.telephony.callback_urls import (
    exotel_callback_token,
    exotel_stream_token,
    media_stream_url,
    status_callback_url,
    valid_exotel_callback_token,
    valid_exotel_stream_token,
)
from app.telephony.metrics import CallMetrics
from tests import fixtures


class FakeSocket:
    """Collects what the adapter sends and replays a scripted inbound stream."""

    def __init__(self, inbound: list[str] | None = None) -> None:
        self.sent: list[dict] = []
        self._inbound = list(inbound or [])
        self.closed = False

    async def send_text(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def receive_text(self) -> str:
        if not self._inbound:
            raise AssertionError("inbound script exhausted")
        return self._inbound.pop(0)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    def of_type(self, event: str) -> list[dict]:
        return [message for message in self.sent if message.get("event") == event]


def media_message(pcm: bytes) -> str:
    return json.dumps({
        "event": "media",
        "sequence_number": 3,
        "stream_sid": "MZ123",
        "media": {"chunk": 1, "timestamp": "10", "payload": base64.b64encode(pcm).decode()},
    })


def adapter(inbound: list[str] | None = None, chunk_bytes: int = 3200, metrics=None) -> ExotelAdapter:
    built = ExotelAdapter(audio_bridge=object(), metrics=metrics, send_chunk_bytes=chunk_bytes)
    built.websocket = FakeSocket(inbound)
    built.stream_sid = "MZ123"
    built.call_sid = "exotel-sid-1"
    return built


# ---------------------------------------------------------------------------
# Chunk alignment
# ---------------------------------------------------------------------------
def test_the_chunk_size_is_always_a_multiple_of_320():
    """Exotel's one unambiguous constraint. An unaligned chunk produces the
    audio gaps and timing drift its docs warn about."""
    for requested in (1, 100, 319, 320, 321, 3200, 3300, 100_000):
        assert _aligned_chunk_bytes(requested) % 320 == 0
        assert _aligned_chunk_bytes(requested) >= 320


def test_a_misconfigured_chunk_size_rounds_down_rather_than_breaking_alignment():
    assert _aligned_chunk_bytes(3300) == 3200
    assert _aligned_chunk_bytes(0) == 320


def test_one_mulaw_frame_is_exactly_one_exotel_frame():
    assert PCM_FRAME_BYTES == MULAW_FRAME_BYTES * 2 == 320


# ---------------------------------------------------------------------------
# Outbound framing
# ---------------------------------------------------------------------------
def test_outbound_audio_is_batched_into_aligned_chunks():
    exotel = adapter(chunk_bytes=3200)
    frames = fixtures.frames(fixtures.tone(6000, 200))  # 10 x 20ms

    async def scenario():
        for frame in frames:
            assert await exotel.send_audio(frame)

    asyncio.run(scenario())

    media = exotel.websocket.of_type("media")
    assert len(media) == 1, "200ms at 3200 bytes/chunk is exactly one message"
    payload = base64.b64decode(media[0]["media"]["payload"])
    assert len(payload) == 3200 and len(payload) % 320 == 0


def test_a_partial_chunk_is_held_rather_than_sent_short():
    exotel = adapter(chunk_bytes=3200)

    async def scenario():
        for frame in fixtures.frames(fixtures.tone(6000, 100)):  # only 5 frames
            await exotel.send_audio(frame)

    asyncio.run(scenario())

    assert exotel.websocket.of_type("media") == [], "an undersized chunk must not be sent"


def test_outbound_messages_use_exotels_snake_case_stream_key():
    """Exotel says stream_sid; Twilio says streamSid. Sending the wrong one
    means the carrier silently ignores every message."""
    exotel = adapter(chunk_bytes=320)

    asyncio.run(exotel.send_audio(fixtures.silence(20)))

    message = exotel.websocket.of_type("media")[0]
    assert "stream_sid" in message and "streamSid" not in message
    assert message["stream_sid"] == "MZ123"


def test_clear_and_mark_also_use_snake_case_and_clear_drops_the_pending_batch():
    exotel = adapter(chunk_bytes=3200)

    async def scenario():
        await exotel.send_audio(fixtures.silence(20))  # sits in the batch buffer
        assert exotel._outbound_pcm
        await exotel.clear_playback()

    asyncio.run(scenario())

    clear = exotel.websocket.of_type("clear")[0]
    assert "stream_sid" in clear and "streamSid" not in clear
    assert not exotel._outbound_pcm, "buffered audio must not survive a barge-in"
    assert not exotel._pending_marks


def test_a_mark_is_sent_once_per_wire_chunk():
    exotel = adapter(chunk_bytes=640)  # two frames per chunk

    async def scenario():
        for frame in fixtures.frames(fixtures.tone(6000, 80)):  # 4 frames -> 2 chunks
            await exotel.send_audio(frame)

    asyncio.run(scenario())

    assert len(exotel.websocket.of_type("media")) == 2
    assert len(exotel.websocket.of_type("mark")) == 2


def test_an_acknowledged_mark_stops_counting_as_playing():
    exotel = adapter([json.dumps({"event": "mark", "stream_sid": "MZ123", "mark": {"name": "m1"}}),
                      json.dumps({"event": "stop", "stream_sid": "MZ123"})], chunk_bytes=320)

    async def scenario():
        await exotel.send_audio(fixtures.silence(20))
        assert exotel.audio_currently_playing, "unacknowledged mark means still playing"
        assert await exotel.receive_audio() is None  # consumes the mark, then stop
        assert not exotel.audio_currently_playing

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Inbound framing and transcoding
# ---------------------------------------------------------------------------
def test_inbound_chunks_are_split_into_twenty_millisecond_mulaw_frames():
    """Every barge-in constant is expressed in 20ms frames. Handing the
    tracker a 200ms frame would silently rescale all of them."""
    original = b"".join(fixtures.frames(fixtures.tone(6000, 200)))
    exotel = adapter([media_message(g711.ulaw_to_pcm16(original))])

    async def scenario():
        return [await exotel.receive_audio() for _ in range(10)]

    received = asyncio.run(scenario())

    assert all(len(frame) == MULAW_FRAME_BYTES for frame in received)
    assert b"".join(received) == original, "audio must survive the split unchanged"


def test_audio_round_trips_through_the_exotel_framing_unchanged():
    """Out through the wire format and back in again, byte for byte."""
    exotel = adapter(chunk_bytes=3200)
    original = b"".join(fixtures.frames(fixtures.tone(7000, 200)))

    async def scenario():
        for frame in fixtures.frames(original):
            await exotel.send_audio(frame)

    asyncio.run(scenario())
    on_the_wire = base64.b64decode(exotel.websocket.of_type("media")[0]["media"]["payload"])

    returning = adapter([media_message(on_the_wire)])

    async def read_back():
        return [await returning.receive_audio() for _ in range(10)]

    assert b"".join(asyncio.run(read_back())) == original


def test_stop_ends_the_stream():
    exotel = adapter([json.dumps({"event": "stop", "stream_sid": "MZ123", "stop": {"reason": "callended"}})])
    assert asyncio.run(exotel.receive_audio()) is None


def test_an_unaligned_inbound_chunk_is_padded_rather_than_desynchronising():
    """Exotel promises multiples of 320. If it ever breaks that promise, a
    short frame would shift the tracker's frame-count arithmetic for the rest
    of the call."""
    exotel = adapter([media_message(g711.ulaw_to_pcm16(fixtures.silence(20)) + b"\x00\x02")])

    frame = asyncio.run(exotel.receive_audio())

    assert len(frame) == MULAW_FRAME_BYTES
    assert len(asyncio.run(exotel.receive_audio())) == MULAW_FRAME_BYTES


# ---------------------------------------------------------------------------
# The metrics clock through the re-framer
# ---------------------------------------------------------------------------
def test_reframed_frames_are_timestamped_at_arrival_not_at_emission():
    """A chunk's last sample arrived now; everything before it is older. If
    every sub-frame were stamped "now", the caller's end-of-turn would land up
    to a whole chunk late and eot_to_first_audio_ms would read low."""
    observed: list[tuple[bytes, float]] = []

    class RecordingMetrics:
        def observe_inbound(self, frame, at=None):
            observed.append((frame, at))

    exotel = ExotelAdapter(audio_bridge=object(), metrics=RecordingMetrics())
    exotel.websocket = FakeSocket([media_message(g711.ulaw_to_pcm16(b"".join(fixtures.frames(fixtures.tone(6000, 200)))))])
    exotel.stream_sid = "MZ123"

    async def scenario():
        for _ in range(10):
            await exotel.receive_audio()

    before = time.monotonic()
    asyncio.run(scenario())
    after = time.monotonic()

    stamps = [at for _frame, at in observed]
    assert len(stamps) == 10
    assert stamps == sorted(stamps), "frames must be stamped in audio order"
    # 10 frames spanning 200ms: the first is ~180ms older than the last.
    assert stamps[-1] - stamps[0] == pytest.approx(0.18, abs=0.001)
    assert before <= stamps[-1] <= after, "the last frame is the one that just arrived"


def test_exotel_and_twilio_report_comparable_latency_for_the_same_conversation():
    """The point of carrying the timestamp: the same audio, delivered in one
    20ms message by Twilio or one 200ms chunk by Exotel, must produce the same
    eot_to_first_audio_ms. Otherwise the two carriers cannot be compared.
    """
    speech = b"".join(fixtures.frames(fixtures.tone(8000, 200)))

    def measure(adapter_under_test, reads: int) -> float:
        metrics = CallMetrics("call-x", lambda events: None, voice_threshold=100)
        metrics.bind()
        adapter_under_test.metrics = metrics

        async def scenario():
            for _ in range(reads):
                await adapter_under_test.receive_audio()

        asyncio.run(scenario())
        # Time from the caller's last voiced frame to "now" -- what
        # eot_to_first_audio_ms is measured from.
        return time.monotonic() - metrics._last_voiced_at

    exotel = adapter([media_message(g711.ulaw_to_pcm16(speech))])
    twilio = TwilioAdapter(audio_bridge=object(), client=object())
    twilio.stream_sid = "MZ123"
    twilio.websocket = FakeSocket([
        json.dumps({"event": "media", "streamSid": "MZ123",
                    "media": {"payload": base64.b64encode(frame).decode()}})
        for frame in fixtures.frames(speech)
    ])

    exotel_age = measure(exotel, reads=10)
    twilio_age = measure(twilio, reads=10)

    # Both should mark end-of-turn at essentially "just now". Without the
    # carried timestamp Exotel's would be indistinguishable here too -- what
    # would differ is a mid-chunk end of speech, covered by the test above.
    assert abs(exotel_age - twilio_age) < 0.02


# ---------------------------------------------------------------------------
# Stream and callback tokens
# ---------------------------------------------------------------------------
@pytest.fixture()
def secret(monkeypatch):
    monkeypatch.setattr("app.telephony.callback_urls.STREAM_SECRET", "test-secret-not-real")
    return "test-secret-not-real"


def test_the_stream_token_round_trips_through_the_query_string(secret):
    url = media_stream_url("exotel", "call-9")
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)

    assert query["call_id"] == ["call-9"]
    assert valid_exotel_stream_token("call-9", int(query["expiry"][0]), query["token"][0])


def test_the_stream_url_fits_exotels_three_parameter_and_256_character_limits(secret):
    """Exotel allows at most 3 custom params totalling under 256 characters,
    and a streamurl under 600. This is why the HMAC needs no truncation."""
    from urllib.parse import urlparse

    url = media_stream_url("exotel", "3f1c8a26-4d5e-4b7a-9c21-8e0f6b5a2d47")
    query = urlparse(url).query

    assert len(query.split("&")) == 3
    assert len(query) < 256
    assert len(url) < 600


def test_an_expired_stream_token_is_rejected(secret):
    expired = int(time.time()) - 1
    assert not valid_exotel_stream_token("call-9", expired, exotel_stream_token("call-9", expired))


def test_a_tampered_stream_token_is_rejected(secret):
    expiry = int(time.time()) + 300
    good = exotel_stream_token("call-9", expiry)

    assert not valid_exotel_stream_token("call-9", expiry, good[:-1] + ("0" if good[-1] != "0" else "1"))
    assert not valid_exotel_stream_token("call-OTHER", expiry, good), "a token is bound to its call"
    assert not valid_exotel_stream_token("call-9", expiry + 1, good), "and to its expiry"
    assert not valid_exotel_stream_token("call-9", expiry, "")


def test_a_blank_secret_rejects_every_token(monkeypatch):
    """The failure that once dropped every call two seconds after pickup."""
    monkeypatch.setattr("app.telephony.callback_urls.STREAM_SECRET", "")
    expiry = int(time.time()) + 300
    assert exotel_stream_token("call-9", expiry) == ""
    assert not valid_exotel_stream_token("call-9", expiry, "")
    assert not valid_exotel_stream_token("call-9", expiry, "anything")


def test_stream_and_callback_tokens_are_domain_separated(secret):
    """Different lifetimes and different blast radii: one must never validate
    for the other."""
    expiry = int(time.time()) + 300

    assert exotel_stream_token("call-9", expiry) != exotel_callback_token("call-9", expiry)
    assert not valid_exotel_callback_token("call-9", expiry, exotel_stream_token("call-9", expiry))
    assert not valid_exotel_stream_token("call-9", expiry, exotel_callback_token("call-9", expiry))


def test_the_callback_token_outlives_the_call(secret):
    from urllib.parse import parse_qs, urlparse

    from app.core.settings import MAX_CALL_SECONDS

    query = parse_qs(urlparse(status_callback_url("exotel", "call-9")).query)
    assert int(query["expiry"][0]) > time.time() + MAX_CALL_SECONDS


def test_twilio_urls_are_untouched_by_any_of_this(secret):
    assert media_stream_url("twilio", "call-9").endswith("/media-stream")
    assert "token=" not in media_stream_url("twilio", "call-9")
    assert status_callback_url("twilio", "call-9").endswith("/twilio/status/call-9")
