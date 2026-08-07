from __future__ import annotations

import json
import time

import pytest

from tests import fixtures
from app.telephony.audio.local_vad import rms_energy, _MULAW_DECODE_TABLE
from app.telephony.metrics import CallMetrics, percentile


def collected(metrics: CallMetrics) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for name, payload in metrics.drain():
        grouped.setdefault(name, []).append(payload)
    return grouped


def test_rms_energy_matches_the_reference_per_sample_loop():
    """The table-driven RMS must stay bit-identical to the obvious version.

    It is the input to every barge-in decision and every acoustic histogram,
    so an optimisation that shifted it even slightly would silently retune
    the whole system.
    """
    for frame in (fixtures.silence(), fixtures.noise(900), fixtures.tone(8000), bytes(range(256))):
        expected = (sum(_MULAW_DECODE_TABLE[b] ** 2 for b in frame) / len(frame)) ** 0.5
        assert rms_energy(frame) == expected


def test_rms_energy_of_empty_frame_is_zero():
    assert rms_energy(b"") == 0.0


def test_fixture_conditions_sit_where_the_tests_assume():
    """Guards the fixtures themselves: a silent regression here would make
    every acoustic test below meaningless while still passing."""
    assert rms_energy(fixtures.quiet_line()[0]) < 400
    assert rms_energy(fixtures.noisy_line()[0]) > 400
    assert rms_energy(fixtures.backchannel()[0]) > 400
    assert rms_energy(fixtures.speakerphone_echo()[0]) > 400


def test_greeting_latency_is_measured_from_bind_to_first_outbound_byte():
    metrics = CallMetrics("call-1")
    metrics.bind()
    time.sleep(0.02)
    metrics.observe_outbound(fixtures.tone(8000))

    events = collected(metrics)
    assert len(events["metrics_greeting"]) == 1
    assert events["metrics_greeting"][0]["bind_to_first_audio_ms"] >= 20


def test_turn_latency_is_measured_from_last_voiced_caller_frame():
    metrics = CallMetrics("call-2")
    metrics.bind()
    metrics.observe_outbound(fixtures.tone(8000))  # greeting

    for frame in fixtures.genuine_interruption(200):
        metrics.observe_inbound(frame)
    time.sleep(0.05)
    metrics.agent_turn_started()
    metrics.observe_outbound(fixtures.tone(8000))

    turns = collected(metrics)["metrics_turn"]
    assert len(turns) == 1
    assert turns[0]["turn"] == 1
    assert turns[0]["source"] == "provider"
    assert turns[0]["eot_to_first_audio_ms"] >= 50
    assert "provider_signal_to_first_audio_ms" in turns[0]


def test_continuous_agent_audio_is_one_turn_not_many():
    """Every 20ms frame of a single reply must not register as a new turn."""
    metrics = CallMetrics("call-3")
    metrics.bind()
    for frame in fixtures.frames(fixtures.tone(8000, ms=400)):
        metrics.observe_outbound(frame)

    events = collected(metrics)
    assert len(events["metrics_greeting"]) == 1
    assert "metrics_turn" not in events


def test_provider_latency_report_is_converted_to_milliseconds():
    metrics = CallMetrics("call-4")
    metrics.latency_report({
        "stt_latency": 0.12,
        "ttt_text_latency": 0.34,
        "tts_latency": 0.08,
        "total_latency": 0.54,
        "ttt_thinking_latency": None,
    })
    payload = collected(metrics)["metrics_provider_latency"][0]
    assert payload["stt_ms"] == 120.0
    assert payload["llm_first_text_ms"] == 340.0
    assert payload["tts_ttfb_ms"] == 80.0
    assert payload["provider_total_ms"] == 540.0
    assert "ttt_thinking_latency" not in payload


def test_barge_in_decisions_record_the_evidence_behind_them():
    metrics = CallMetrics("call-5", voice_threshold=400.0)
    metrics.barge_in_decision("resume", rms=612.5, voiced_ms=40, elapsed_ms=180.4, frames=9, agent_playing=True)
    metrics.barge_in_decision("commit", rms=9000.0, voiced_ms=560, elapsed_ms=600.0, frames=30, agent_playing=True)

    events = collected(metrics)["metrics_barge_in"]
    assert [e["decision"] for e in events] == ["resume", "commit"]
    assert events[0]["rms"] == 612.5
    assert events[0]["threshold"] == 400.0
    assert events[1]["voiced_ms"] == 560


def test_short_quiet_stretches_are_not_counted_as_dead_air():
    metrics = CallMetrics("call-6", silence_gap_ms=1500)
    metrics.bind()
    for frame in fixtures.quiet_line(400):
        metrics.observe_inbound(frame)
    metrics.observe_inbound(fixtures.genuine_interruption(20)[0])

    events = {name: payload for name, payload in metrics.finish("completed")}
    assert events["metrics_call"]["silence_gaps"] == 0


def test_silence_accounting_counts_only_gaps_at_or_over_the_threshold():
    metrics = CallMetrics("call-7", silence_gap_ms=50)
    metrics.bind()
    # Quiet frames are measured against a wall clock, not frame count, so
    # drive real elapsed time rather than pretending 20ms per frame.
    metrics.observe_inbound(fixtures.quiet_line(20)[0])
    time.sleep(0.06)
    metrics.observe_inbound(fixtures.genuine_interruption(20)[0])

    events = {name: payload for name, payload in metrics.finish("completed")}
    assert events["metrics_call"]["silence_gaps"] == 1
    assert events["metrics_call"]["silence_total_ms"] >= 50


def test_call_summary_reports_cost_inputs_and_rounds_billing_up_to_the_minute():
    metrics = CallMetrics("call-8")
    metrics.bind()
    metrics.assistant_characters(120)
    metrics.assistant_characters(80)
    metrics.user_message()

    events = {name: payload for name, payload in metrics.finish("completed")}
    summary = events["metrics_call"]
    assert summary["tts_characters"] == 200
    assert summary["agent_messages"] == 2
    assert summary["user_messages"] == 1
    assert summary["media_end_reason"] == "completed"
    # Twilio bills voice per started minute; a two-second call still costs 60s.
    assert summary["billable_seconds"] == 60


def test_acoustic_histograms_separate_agent_silent_from_agent_speaking():
    """The two distributions are the whole point: a threshold has to clear
    the quiet-line floor, and echo only ever appears in the other one."""
    metrics = CallMetrics("call-9")
    metrics.bind()
    for frame in fixtures.quiet_line(200):
        metrics.observe_inbound(frame)

    agent_frame = fixtures.tone(9000)
    for frame in fixtures.speakerphone_echo(ms=200):
        metrics.observe_outbound(agent_frame)
        metrics.observe_inbound(frame)

    events = {name: payload for name, payload in metrics.finish("completed")}
    acoustics = events["metrics_acoustics"]
    assert acoustics["rms_agent_silent"], "quiet-line frames must be bucketed"
    assert acoustics["rms_agent_speaking"], "echo frames must be bucketed separately"
    assert acoustics["caller_over_agent_db"], "echo ratio must be recorded while agent audio plays"
    # Echo is attenuated agent audio, so it must land below 0 dB relative to it.
    assert all(float(bucket) < 0 for bucket in acoustics["caller_over_agent_db"] if bucket != "<=-30")


def test_metrics_never_carry_transcript_text():
    """`assistant_characters` takes an int by design; this pins that shape so
    a future change cannot start passing the utterance itself."""
    metrics = CallMetrics("call-10")
    metrics.bind()
    metrics.assistant_characters(len("नमस्ते, मैं Shruti बोल रही हूँ"))
    metrics.barge_in_decision("commit", rms=1.0, voiced_ms=1, elapsed_ms=1.0, frames=1, agent_playing=False)
    serialized = json.dumps([payload for _, payload in metrics.finish("completed")], ensure_ascii=False)
    assert "Shruti" not in serialized
    assert "नमस्ते" not in serialized


def test_percentile_uses_nearest_rank_and_handles_empty_input():
    assert percentile([], 0.5) is None
    assert percentile([10.0], 0.99) == 10.0
    values = [float(n) for n in range(1, 101)]
    assert percentile(values, 0.50) == 50.0
    assert percentile(values, 0.90) == 90.0
    assert percentile(values, 0.99) == 99.0
