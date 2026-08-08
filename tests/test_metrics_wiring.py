from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from tests import fixtures
from app.core.settings import BARGE_IN_CONFIRM_MS, BARGE_IN_VOICE_ENERGY_THRESHOLD, TWILIO_FRAME_MS
from app.integrations.twilio_media import encode_media_payload
from app.storage.sqlite_store import SQLiteCallStore
from app.telephony.adapters.twilio_adapter import TwilioAdapter
from app.telephony.audio.media_dump import MediaDump
from app.telephony.audio.paced_sender import PacedSender
from app.telephony.metrics import CallMetrics, MetricsWriter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.call_metrics import build_report, load_events  # noqa: E402


class FakeWebSocket:
    """Minimal stand-in for Twilio's media WebSocket."""

    def __init__(self, inbound: list[str] | None = None) -> None:
        self.inbound = list(inbound or [])
        self.sent: list[dict] = []

    async def receive_text(self) -> str:
        if not self.inbound:
            raise AssertionError("test consumed more inbound messages than it queued")
        return self.inbound.pop(0)

    async def send_text(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def media_message(frame: bytes) -> str:
    return json.dumps({"event": "media", "media": {"payload": encode_media_payload(frame)}})


def build_adapter(metrics: CallMetrics, inbound: list[str] | None = None) -> TwilioAdapter:
    adapter = TwilioAdapter(audio_bridge=object(), client=object(), metrics=metrics)
    adapter.websocket = FakeWebSocket(inbound)
    adapter.stream_sid = "MZtest"
    return adapter


def events_by_name(metrics: CallMetrics) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for name, payload in metrics.drain():
        grouped.setdefault(name, []).append(payload)
    return grouped


def test_outbound_frames_are_measured_where_they_leave_for_twilio():
    metrics = CallMetrics("call-a")
    metrics.bind()
    adapter = build_adapter(metrics)

    assert asyncio.run(adapter.send_audio(fixtures.tone(8000))) is True

    assert adapter.websocket.sent[0]["event"] == "media"
    assert "metrics_greeting" in events_by_name(metrics)


def test_inbound_frames_are_measured_as_they_arrive():
    metrics = CallMetrics("call-b")
    metrics.bind()
    frame = fixtures.genuine_interruption(20)[0]
    adapter = build_adapter(metrics, [media_message(frame)])

    assert asyncio.run(adapter.receive_audio()) == frame

    summary = {name: payload for name, payload in metrics.finish("completed")}
    assert summary["metrics_call"]["inbound_frames"] == 1
    assert summary["metrics_call"]["voiced_frames"] == 1


def test_a_sustained_interruption_is_committed_and_logged_with_its_evidence():
    """The decision and the reason for it have to land together: a commit
    with no RMS attached cannot be told apart from one driven by line noise.
    """
    committed: list[bool] = []

    class Bridge:
        def commit_interruption(self) -> None:
            committed.append(True)

    metrics = CallMetrics("call-c", voice_threshold=BARGE_IN_VOICE_ENERGY_THRESHOLD)
    metrics.bind()
    adapter = build_adapter(metrics)
    adapter.audio_bridge = Bridge()
    adapter._paced_sender = PacedSender(adapter.send_audio)
    adapter._begin_soft_pause()

    for frame in fixtures.genuine_interruption(BARGE_IN_CONFIRM_MS + 4 * TWILIO_FRAME_MS):
        adapter._process_barge_in_signal(frame)

    assert committed == [True]
    decisions = events_by_name(metrics)["metrics_barge_in"]
    assert [d["decision"] for d in decisions] == ["commit"]
    assert decisions[0]["rms"] > BARGE_IN_VOICE_ENERGY_THRESHOLD
    assert decisions[0]["voiced_ms"] >= BARGE_IN_CONFIRM_MS


def test_a_brief_backchannel_resumes_playback_and_is_logged_as_such():
    metrics = CallMetrics("call-d", voice_threshold=BARGE_IN_VOICE_ENERGY_THRESHOLD)
    metrics.bind()
    adapter = build_adapter(metrics)
    adapter.audio_bridge = object()
    adapter._paced_sender = PacedSender(adapter.send_audio)
    adapter._paced_sender.feed(fixtures.tone(8000))
    adapter._begin_soft_pause()

    for frame in fixtures.backchannel(120) + fixtures.quiet_line(200):
        adapter._process_barge_in_signal(frame)

    decisions = events_by_name(metrics)["metrics_barge_in"]
    assert [d["decision"] for d in decisions] == ["resume"]
    # Buffered audio survives a false barge-in; that is the whole point of
    # pausing rather than discarding.
    assert adapter._paced_sender.has_buffered_audio


def test_energy_and_duration_alone_cannot_reject_echo():
    """Why the level comparison in the adapter has to exist.

    Fed to the duration tracker with no knowledge of what the agent was
    playing, speakerphone echo is indistinguishable from a customer: it is
    loud, it is sustained, and it confirms a barge-in. The rejection cannot
    come from this stage, which is what `_is_probable_echo` is for --
    exercised against the adapter in tests/test_barge_in_acoustics.py.
    """
    from app.telephony.audio.local_vad import VoicedDurationTracker
    from app.core.settings import BARGE_IN_CONFIRM_MS, BARGE_IN_HANGOVER_FRAMES, TWILIO_FRAME_MS

    tracker = VoicedDurationTracker(
        BARGE_IN_VOICE_ENERGY_THRESHOLD, TWILIO_FRAME_MS, hangover_frames=BARGE_IN_HANGOVER_FRAMES
    )
    for frame in fixtures.speakerphone_echo(ms=BARGE_IN_CONFIRM_MS + 200):
        tracker.observe(frame)
    assert tracker.voiced_ms >= BARGE_IN_CONFIRM_MS


def test_a_noisy_line_stops_confirming_barge_ins_once_the_floor_has_adapted():
    """The line's own noise used to clear the fixed threshold, so a
    barge-in confirmed with nobody speaking. The floor is learned from
    inbound audio during agent silence, so drive it the way the call loop
    does before judging.
    """
    from app.telephony.audio.local_vad import AdaptiveNoiseFloor, VoicedDurationTracker, rms_energy
    from app.core.settings import (
        BARGE_IN_CONFIRM_MS, BARGE_IN_HANGOVER_FRAMES, BARGE_IN_NOISE_MULTIPLIER, TWILIO_FRAME_MS,
    )

    floor = AdaptiveNoiseFloor(
        multiplier=BARGE_IN_NOISE_MULTIPLIER, minimum=BARGE_IN_VOICE_ENERGY_THRESHOLD
    )
    for frame in fixtures.noisy_line(20000):
        floor.observe(rms_energy(frame), False)

    tracker = VoicedDurationTracker(
        BARGE_IN_VOICE_ENERGY_THRESHOLD, TWILIO_FRAME_MS,
        hangover_frames=BARGE_IN_HANGOVER_FRAMES, threshold_provider=lambda: floor.threshold,
    )
    for frame in fixtures.noisy_line(BARGE_IN_CONFIRM_MS + 400):
        tracker.observe(frame)
    assert tracker.voiced_ms < BARGE_IN_CONFIRM_MS, "line noise must no longer confirm a barge-in"


def test_media_dump_keeps_both_directions_time_aligned(tmp_path):
    """Agent audio is sparse; without padding the two files drift and the
    capture cannot answer "what was playing when this fired"."""
    dump = MediaDump.create(tmp_path, "call-g")
    assert dump is not None
    silent_prefix = fixtures.quiet_line(200)
    for frame in silent_prefix:
        dump.write_inbound(frame)
    # One agent frame while the caller keeps streaming, as a live call does.
    for frame in fixtures.quiet_line(200):
        dump.write_inbound(frame)
        dump.write_outbound(fixtures.tone(8000))
    dump.close()

    inbound = (tmp_path / "call-g.in.ulaw").read_bytes()
    outbound = (tmp_path / "call-g.out.ulaw").read_bytes()
    assert abs(len(inbound) - len(outbound)) <= fixtures.FRAME_BYTES
    # The agent was silent for the first 200ms, and the capture has to show
    # that as silence rather than compressing it away.
    prefix_bytes = sum(len(f) for f in silent_prefix)
    assert outbound[:prefix_bytes] == bytes([0xFF]) * prefix_bytes


def test_media_dump_is_off_unless_a_directory_is_configured():
    assert MediaDump.create(None, "call-h") is None


def test_metrics_reach_call_events_and_the_analysis_pass_reads_them(tmp_path):
    store = SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)
    call = store.enqueue_call(phone_number="+919000000001", business_name="Test Traders")
    call_id = call["call_id"]

    metrics = CallMetrics(call_id)
    metrics.bind()
    metrics.latency_report({"stt_latency": 0.1, "ttt_text_latency": 0.3, "tts_latency": 0.09, "total_latency": 0.49})
    metrics.barge_in_decision("commit", rms=5000.0, voiced_ms=560, elapsed_ms=600.0, frames=30, agent_playing=True)
    metrics.assistant_characters(140)
    pending = metrics.drain() + metrics.finish("completed")

    assert store.record_events(call_id, pending) == len(pending)

    stored = store.list_events(call_id, "metrics_")
    assert {row["event_name"] for row in stored} >= {
        "metrics_bound", "metrics_provider_latency", "metrics_barge_in", "metrics_call", "metrics_acoustics",
    }

    report = build_report(load_events(tmp_path / "calls.sqlite3", [call_id], None, 10))
    assert report["latency"]["Deepgram STT (per partial, see note)"]["p50"] == 100.0
    assert report["latency"]["TTS time to first byte"]["p50"] == 90.0
    assert report["barge_in"]["decisions"] == {"commit": 1}
    assert report["cost"]["tts_characters_total"] == 140


def test_record_events_never_raises_for_a_call_that_no_longer_exists(tmp_path):
    """The metrics writer flushes asynchronously; an operator deleting a call
    mid-flush must not surface as an exception on the call path."""
    store = SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)
    assert store.record_events("does-not-exist", [("metrics_call", {"media_seconds": 1})]) == 0


def test_metrics_writer_flushes_from_a_background_task(tmp_path):
    store = SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)
    call_id = store.enqueue_call(phone_number="+919000000002")["call_id"]

    async def scenario() -> None:
        writer = MetricsWriter(store, call_id, flush_interval=0.01)
        writer.start()
        writer.sink([("metrics_turn", {"eot_to_first_audio_ms": 640.0})])
        await asyncio.sleep(0.05)
        await writer.stop()

    asyncio.run(scenario())
    stored = store.list_events(call_id, "metrics_turn")
    assert json.loads(stored[0]["metadata"])["eot_to_first_audio_ms"] == 640.0


def test_metrics_sink_failures_never_propagate_into_the_call():
    def exploding_sink(_events):
        raise RuntimeError("storage is down")

    metrics = CallMetrics("call-i", exploding_sink, flush_every=1)
    metrics.bind()
    metrics.observe_outbound(fixtures.tone(8000))  # triggers a flush
    metrics.finish("completed")
