from __future__ import annotations

import pytest

from tests import fixtures
from app.core.settings import (
    BARGE_IN_CONFIRM_MS,
    BARGE_IN_ECHO_MARGIN,
    BARGE_IN_HANGOVER_FRAMES,
    BARGE_IN_VOICE_ENERGY_THRESHOLD,
    TWILIO_FRAME_MS,
)
from app.telephony.audio.local_vad import AdaptiveNoiseFloor, VoicedDurationTracker, rms_energy
from app.telephony.adapters.twilio_adapter import TwilioAdapter
from app.telephony.audio.paced_sender import PacedSender
from app.telephony.metrics import CallMetrics


# ---------------------------------------------------------------------------
# Adaptive noise floor
# ---------------------------------------------------------------------------
def settle(floor: AdaptiveNoiseFloor, frames: list[bytes], agent_playing: bool = False) -> None:
    for frame in frames:
        floor.observe(rms_energy(frame), agent_playing)


def test_the_floor_falls_to_a_quiet_line():
    floor = AdaptiveNoiseFloor(initial_floor=500.0)
    settle(floor, fixtures.quiet_line(3000))
    assert floor.floor < 120, "a quiet line must pull the floor down"
    assert floor.threshold == BARGE_IN_VOICE_ENERGY_THRESHOLD, "clamped at the configured minimum"


def test_the_floor_rises_on_a_noisy_line_so_noise_stops_reading_as_speech():
    """The failure this exists to prevent: on a line whose noise floor sits
    above a fixed threshold, every frame reads as voiced and a barge-in
    confirms with nobody speaking."""
    floor = AdaptiveNoiseFloor(initial_floor=60.0)
    settle(floor, fixtures.noisy_line(20000))
    noise_rms = rms_energy(fixtures.noisy_line()[0])
    assert floor.threshold > noise_rms, "the threshold must end up above the line's own noise"


def test_the_floor_ignores_audio_arriving_while_the_agent_speaks():
    """Inbound audio during agent speech is largely the agent's own voice
    returning. Folding it in would make the threshold chase the echo."""
    floor = AdaptiveNoiseFloor(initial_floor=60.0)
    settle(floor, fixtures.speakerphone_echo(ms=4000), agent_playing=True)
    assert floor.floor == pytest.approx(60.0), "no sample may be taken while agent audio plays"
    assert floor.samples == 0


def test_speech_cannot_ratchet_the_floor_upward():
    """The regression this class exists in its current form to prevent.

    The threshold is a multiple of the floor, so any rule that lets a loud
    frame pull the floor toward its own level is self-reinforcing: each rise
    widens the band of frames able to raise it further. Two earlier versions
    of this estimator ran away to the clamp inside a single call and went
    deaf to the customer mid-utterance. Loud frames may creep the floor, but
    never toward how loud they were.
    """
    floor = AdaptiveNoiseFloor(initial_floor=42.0)
    quiet = fixtures.quiet_line(2000)
    speech = fixtures.genuine_interruption(4000)
    for _ in range(6):
        settle(floor, quiet)
        settle(floor, speech)
    assert floor.floor < 120, f"floor ran away to {floor.floor:.0f} across alternating speech"
    assert floor.threshold < 800, "the threshold must stay well under normal speech level"


def test_a_line_that_genuinely_gets_noisier_is_still_tracked():
    """The cost of ignoring loud frames must not be an estimator that can
    never recover when the line itself changes."""
    floor = AdaptiveNoiseFloor(initial_floor=40.0)
    settle(floor, fixtures.noisy_line(20000))
    assert floor.threshold > rms_energy(fixtures.noisy_line()[0])


def test_the_threshold_is_clamped_at_both_ends():
    """Neither extreme may silently disable barge-in altogether."""
    silent = AdaptiveNoiseFloor(initial_floor=0.0, minimum=250.0)
    assert silent.threshold == 250.0
    deafening = AdaptiveNoiseFloor(initial_floor=100000.0, maximum=4000.0)
    assert deafening.threshold == 4000.0


# ---------------------------------------------------------------------------
# The hangover fix -- the reason genuine interruptions were ignored
# ---------------------------------------------------------------------------
def voiced_runs(frames: list[bytes], hangover: int, threshold: float = 400.0) -> list[int]:
    tracker = VoicedDurationTracker(threshold, TWILIO_FRAME_MS, hangover_frames=hangover)
    runs, previous = [], 0
    for frame in frames:
        tracker.observe(frame)
        if tracker.voiced_ms == 0 and previous:
            runs.append(previous)
        previous = tracker.voiced_ms
    if previous:
        runs.append(previous)
    return runs


def speech_with_syllable_gaps(syllables: int = 8) -> list[bytes]:
    """Speech as it actually arrives: voiced bursts separated by short gaps.

    Ordinary gaps between syllables run past 60ms, which is what made the
    previous 3-frame hangover shred a single utterance into fragments that
    never individually reached the confirm threshold.
    """
    frames: list[bytes] = []
    for _ in range(syllables):
        frames += fixtures.frames(fixtures.tone(8000, ms=160))
        frames += fixtures.frames(fixtures.silence(ms=100))
    return frames


def test_a_short_hangover_shatters_one_utterance_into_fragments():
    runs = voiced_runs(speech_with_syllable_gaps(), hangover=3)
    assert len(runs) >= 8, "the old 60ms hangover splits every syllable into its own run"
    assert max(runs) < BARGE_IN_CONFIRM_MS, (
        "and no fragment reaches the confirm threshold, so the interruption is ignored"
    )


def test_the_configured_hangover_keeps_one_utterance_intact():
    runs = voiced_runs(speech_with_syllable_gaps(), hangover=BARGE_IN_HANGOVER_FRAMES)
    assert len(runs) == 1, "syllable gaps must not end a run"
    assert runs[0] >= BARGE_IN_CONFIRM_MS, "a real utterance must confirm a barge-in"


def test_a_backchannel_still_stays_below_the_confirm_threshold():
    """The other half of the requirement: don't stop for a simple "acha"."""
    for length in (150, 250, 350):
        runs = voiced_runs(
            fixtures.frames(fixtures.tone(6000, ms=length)) + fixtures.frames(fixtures.silence(ms=400)),
            hangover=BARGE_IN_HANGOVER_FRAMES,
        )
        assert max(runs) < BARGE_IN_CONFIRM_MS, f"a {length}ms backchannel must not interrupt"


def test_the_tracker_follows_a_moving_threshold():
    floor = AdaptiveNoiseFloor(initial_floor=60.0, minimum=100.0)
    tracker = VoicedDurationTracker(400.0, TWILIO_FRAME_MS, threshold_provider=lambda: floor.threshold)
    assert tracker.active_threshold == floor.threshold
    floor.floor = 500.0
    assert tracker.active_threshold == 3000.0


# ---------------------------------------------------------------------------
# End-to-end barge-in decisions through the adapter
# ---------------------------------------------------------------------------
class Bridge:
    def __init__(self) -> None:
        self.committed = False

    def commit_interruption(self) -> None:
        self.committed = True


def adapter_in_pause(agent_playing: bool = False) -> tuple[TwilioAdapter, Bridge, CallMetrics]:
    metrics = CallMetrics("call-x", voice_threshold=BARGE_IN_VOICE_ENERGY_THRESHOLD)
    metrics.bind()
    bridge = Bridge()
    adapter = TwilioAdapter(audio_bridge=bridge, client=object(), metrics=metrics)
    adapter.stream_sid = "MZ"
    adapter._paced_sender = PacedSender(lambda _f: None)
    if agent_playing:
        # Unresolved marks are what audio_currently_playing reads as "playing".
        adapter._pending_marks.add("m1")
        for _ in range(20):
            adapter._recent_agent_rms.append(rms_energy(fixtures.tone(9000)))
    adapter._begin_soft_pause()
    return adapter, bridge, metrics


def feed(adapter: TwilioAdapter, frames: list[bytes]) -> None:
    for frame in frames:
        adapter._process_barge_in_signal(frame)


def test_a_sustained_interruption_stops_the_agent():
    adapter, bridge, _ = adapter_in_pause()
    feed(adapter, fixtures.genuine_interruption(1200))
    assert bridge.committed is True


def test_a_backchannel_does_not_stop_the_agent():
    adapter, bridge, _ = adapter_in_pause()
    feed(adapter, fixtures.backchannel(300) + fixtures.quiet_line(400))
    assert bridge.committed is False
    assert adapter._pause_started_at is None, "playback must have resumed"


def test_speakerphone_echo_no_longer_interrupts_the_agent():
    """Previously this confirmed a barge-in and the agent cut itself off.

    Echo is loud, sustained, and perfectly correlated with agent speech, so
    the duration test alone cannot reject it -- the level comparison must.
    """
    adapter, bridge, _ = adapter_in_pause(agent_playing=True)
    feed(adapter, fixtures.speakerphone_echo(ms=2000))
    assert bridge.committed is False


def test_a_real_customer_still_interrupts_through_playing_agent_audio():
    """The echo filter must not become a way to ignore customers. Someone
    speaking into their handset is far louder than agent leakage."""
    adapter, bridge, _ = adapter_in_pause(agent_playing=True)
    feed(adapter, fixtures.genuine_interruption(1500))
    assert bridge.committed is True


def test_echo_rejection_is_inactive_when_no_agent_audio_is_playing():
    adapter, _, _ = adapter_in_pause(agent_playing=False)
    assert adapter._is_probable_echo(fixtures.quiet_line()[0]) is False


def test_the_echo_margin_stays_biased_toward_hearing_the_customer():
    assert 0 < BARGE_IN_ECHO_MARGIN < 1, "a margin at or above unity would reject real speech"


def test_barge_in_events_record_the_live_adaptive_threshold():
    """The threshold moves during a call, so logging the static constant
    would make the recorded evidence unreadable afterwards."""
    adapter, _, metrics = adapter_in_pause()
    feed(adapter, fixtures.genuine_interruption(1200))
    decisions = [p for name, p in metrics.drain() if name == "metrics_barge_in"]
    assert decisions
    assert decisions[0]["threshold"] == pytest.approx(adapter._noise_floor.threshold, rel=0.01)
