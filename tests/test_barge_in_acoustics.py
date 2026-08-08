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


def adapter_in_pause(
    agent_playing: bool = False, agent_frames: list[bytes] | None = None
) -> tuple[TwilioAdapter, Bridge, CallMetrics]:
    metrics = CallMetrics("call-x", voice_threshold=BARGE_IN_VOICE_ENERGY_THRESHOLD)
    metrics.bind()
    bridge = Bridge()
    adapter = TwilioAdapter(audio_bridge=bridge, client=object(), metrics=metrics)
    adapter.stream_sid = "MZ"
    adapter._paced_sender = PacedSender(lambda _f: None)
    if agent_playing:
        # Unresolved marks are what audio_currently_playing reads as "playing".
        adapter._pending_marks.add("m1")
        played = agent_frames or [fixtures.tone(9000)] * 20
        for frame in played:
            adapter._recent_agent_rms.append(rms_energy(frame))
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


def test_an_echo_only_pause_resumes_quickly_instead_of_timing_out():
    """Rejected echo used to be dropped before the tracker saw it, which left
    `voiced_ms` frozen and the resume check unreachable -- so a pause the
    customer never spoke in could only end on the 2.5s ambiguity timeout.
    That is 2.5 seconds of dead air per false trigger, and it is what the
    last real batch measured: one barge-in decision, and it was a timeout.
    """
    adapter, bridge, metrics = adapter_in_pause(agent_playing=True)
    feed(adapter, fixtures.speakerphone_echo(ms=2000))

    assert bridge.committed is False
    decisions = [p for name, p in metrics.drain() if name == "metrics_barge_in"]
    assert decisions, "the pause must have ended"
    assert decisions[0]["decision"] == "resume"
    assert decisions[0]["elapsed_ms"] < 1000, "and ended promptly, not on the timeout"


def test_a_real_customer_still_interrupts_through_playing_agent_audio():
    """The echo filter must not become a way to ignore customers. Someone
    speaking into their handset is far louder than agent leakage."""
    adapter, bridge, _ = adapter_in_pause(agent_playing=True)
    feed(adapter, fixtures.genuine_interruption(1500))
    assert bridge.committed is True


def test_a_quiet_customer_is_not_mistaken_for_echo_of_a_loud_syllable():
    """The measured failure, and the reason every other test here missed it.

    Every other fixture plays the agent as a constant tone, where a window's
    peak and its mean are the same number -- so comparing the caller against
    either behaved identically and the bug was invisible. Real TTS swings
    between a stressed vowel and the gap after a word, and referencing the
    peak raised the bar on the customer by that whole swing for the entire
    window.

    On a real call this rejected a customer at 2568 RMS speaking over the
    agent. The batch recorded exactly one barge-in decision, a timeout, and a
    commit rate of zero.
    """
    agent = fixtures.agent_speech(400)
    adapter, bridge, _ = adapter_in_pause(agent_playing=True, agent_frames=agent)
    feed(adapter, fixtures.soft_interruption(1500))
    assert bridge.committed is True

    peak = max(rms_energy(frame) for frame in agent)
    caller = rms_energy(fixtures.soft_interruption(20)[0])
    assert caller < peak * BARGE_IN_ECHO_MARGIN, (
        "the fixture must still reproduce the old peak-referenced misclassification, "
        "otherwise this test would pass with the bug present"
    )


def test_echo_through_a_dynamic_agent_signal_is_still_rejected():
    """The mean is a lower reference than the peak, so it has to be shown not
    to have opened the door to the echo this filter exists for."""
    agent = fixtures.agent_speech(400)
    adapter, bridge, _ = adapter_in_pause(agent_playing=True, agent_frames=agent)
    feed(adapter, fixtures.speakerphone_echo(ms=2000))
    assert bridge.committed is False


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


# ---------------------------------------------------------------------------
# Soft mode must actually be in force on the Twilio path
# ---------------------------------------------------------------------------
def test_a_caller_supplied_bridge_is_forced_into_soft_mode():
    """The bug that made every other test in this file irrelevant in production.

    AudioBridge defaults to hard_interrupt=True, the media-stream route
    supplied its own bridge, and TwilioAdapter only set soft mode when it had
    to build one itself. So real calls hard-cut on Deepgram's first
    UserStartedSpeaking: no pause was ever begun, no barge-in decision was
    ever recorded, and any customer sound during the closing line discarded
    the queued goodbye outright.
    """
    from app.telephony.audio.audio_bridge import AudioBridge
    from app.telephony.call_session import CallSession

    session = CallSession(call_id="call-soft", direction="twilio")
    bridge = AudioBridge(session)  # the permissive default
    assert bridge.hard_interrupt is True

    TwilioAdapter(audio_bridge=bridge, client=object()).attach(session)

    assert bridge.hard_interrupt is False


def test_a_bridge_built_by_the_adapter_is_soft_too():
    from app.telephony.call_session import CallSession

    session = CallSession(call_id="call-soft-2", direction="twilio")
    adapter = TwilioAdapter(client=object())
    adapter.attach(session)
    assert adapter.audio_bridge.hard_interrupt is False


def test_soft_mode_queues_a_pause_instead_of_discarding_agent_audio():
    """The behavioural difference that saves the closing line."""
    from app.telephony.audio.audio_bridge import AudioBridge
    from app.telephony.call_session import CallSession

    session = CallSession(call_id="call-soft-3", direction="twilio")
    bridge = AudioBridge(session, hard_interrupt=False)
    bridge.outbound_queue.put_nowait(("audio", b"\xff" * 160))
    bridge._handle_interruption()

    kinds = []
    while not bridge.outbound_queue.empty():
        kinds.append(bridge.outbound_queue.get_nowait()[0])
    assert "audio" in kinds, "queued agent audio must survive an unconfirmed interruption"
    assert "pause" in kinds
    assert "interrupt" not in kinds
