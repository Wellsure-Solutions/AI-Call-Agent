from __future__ import annotations

"""A customer who answers and then says nothing.

Put the phone down, walked away, or a voicemail that answering-machine
detection missed. Until now that call ran to MAX_CALL_SECONDS -- fifteen
minutes of Twilio billing, and at the configured ceiling of one concurrent
call, the entire outbound queue stalled behind somebody who is not there.
"""

import asyncio

import pytest

from app.core.settings import IDLE_NUDGE_LIMIT, IDLE_NUDGE_SECONDS, MAX_CALL_SECONDS
from app.telephony.audio import idle_watcher as idle
from app.telephony.audio.idle_watcher import IdleWatcher
from app.telephony.metrics import CallMetrics


class Clock:
    """A clock that only moves when a test says so."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def watcher(idle_seconds: float = 10.0, max_nudges: int = 3) -> tuple[IdleWatcher, Clock]:
    clock = Clock()
    return IdleWatcher(idle_seconds, max_nudges, clock), clock


def quiet(w: IdleWatcher, clock: Clock, seconds: float, *, agent_playing: bool = False) -> list[str]:
    """Run `seconds` of silence as 20ms frames, collecting every decision."""
    decisions = []
    for _ in range(int(seconds / 0.02)):
        clock.advance(0.02)
        decision = w.observe(caller_voiced=False, agent_playing=agent_playing)
        if decision:
            decisions.append(decision)
    return decisions


# ---------------------------------------------------------------------------
# The escalation
# ---------------------------------------------------------------------------
def test_silence_is_nudged_once_per_idle_window():
    w, clock = watcher(idle_seconds=10.0, max_nudges=3)
    assert quiet(w, clock, 9.0) == [], "under the window, nothing happens"
    assert quiet(w, clock, 2.0) == [idle.NUDGE]
    assert w.nudges == 1


def test_it_gives_up_after_the_configured_number_of_nudges():
    w, clock = watcher(idle_seconds=10.0, max_nudges=3)
    decisions = quiet(w, clock, 60.0)
    assert decisions == [idle.NUDGE, idle.NUDGE, idle.NUDGE, idle.GIVE_UP]


def test_a_dead_line_ends_in_well_under_a_minute():
    """The number that matters: what a silent call now costs."""
    w, clock = watcher(idle_seconds=IDLE_NUDGE_SECONDS, max_nudges=IDLE_NUDGE_LIMIT)
    gave_up_at = None
    for _ in range(int(600 / 0.02)):
        clock.advance(0.02)
        if w.observe(caller_voiced=False, agent_playing=False) == idle.GIVE_UP:
            gave_up_at = clock.now
            break

    assert gave_up_at is not None
    assert gave_up_at <= IDLE_NUDGE_SECONDS * (IDLE_NUDGE_LIMIT + 1) + 1
    assert gave_up_at < MAX_CALL_SECONDS / 10, (
        f"a dead line now costs {gave_up_at:.0f}s instead of {MAX_CALL_SECONDS}s"
    )


def test_nothing_more_is_decided_once_it_has_given_up():
    """The close is already under way; a second give-up would race it."""
    w, clock = watcher(idle_seconds=10.0, max_nudges=1)
    quiet(w, clock, 40.0)
    assert w.gave_up
    assert quiet(w, clock, 120.0) == []


# ---------------------------------------------------------------------------
# When it must stay quiet
# ---------------------------------------------------------------------------
def test_a_customer_who_speaks_resets_everything():
    """Including the strikes: somebody who answers the second nudge deserves
    the same patience as somebody who never went quiet."""
    w, clock = watcher(idle_seconds=10.0, max_nudges=3)
    quiet(w, clock, 25.0)
    assert w.nudges == 2

    clock.advance(0.02)
    assert w.observe(caller_voiced=True, agent_playing=False) == idle.NOTHING
    assert w.nudges == 0
    assert quiet(w, clock, 9.0) == [], "the window starts again from scratch"


def test_the_clock_does_not_run_while_the_agent_is_speaking():
    """A customer listening has not gone away, and nudging over your own
    sentence is worse than saying nothing at all."""
    w, clock = watcher(idle_seconds=10.0, max_nudges=3)
    assert quiet(w, clock, 120.0, agent_playing=True) == []
    assert w.nudges == 0


def test_the_window_starts_fresh_after_the_agent_speaks():
    """Silence before the agent's turn is not carried across it.

    Nine seconds of quiet, then the agent talks: the customer must get a
    whole window to answer, not the one second left of the old one. Anything
    else nudges them a heartbeat after the agent stops -- mid-thought, which
    is precisely when a real caller waits.
    """
    w, clock = watcher(idle_seconds=10.0, max_nudges=3)
    quiet(w, clock, 9.0)
    quiet(w, clock, 30.0, agent_playing=True)
    assert quiet(w, clock, 9.0) == [], "a full window, not the remainder of one"
    assert quiet(w, clock, 2.0) == [idle.NUDGE]


def test_normal_conversation_never_triggers_it():
    """Turn-taking with ordinary thinking pauses must be invisible to this."""
    w, clock = watcher(idle_seconds=IDLE_NUDGE_SECONDS, max_nudges=IDLE_NUDGE_LIMIT)
    for _ in range(12):
        quiet(w, clock, 4.0, agent_playing=True)   # the agent speaks
        quiet(w, clock, 3.0)                        # the customer thinks
        clock.advance(0.02)
        w.observe(caller_voiced=True, agent_playing=False)  # and replies
    assert w.nudges == 0
    assert not w.gave_up


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
class Bridge:
    def __init__(self) -> None:
        self.nudges = 0
        self.closed_for_silence = False

    async def nudge_idle_customer(self) -> bool:
        self.nudges += 1
        return True

    async def close_for_silence(self) -> None:
        self.closed_for_silence = True


def adapter_with_bridge(bridge, metrics=None):
    from app.telephony.adapters.twilio_adapter import TwilioAdapter
    from app.telephony.call_session import CallSession

    adapter = TwilioAdapter(audio_bridge=bridge, client=object(), metrics=metrics)
    adapter.session = CallSession(call_id="call-idle", direction="twilio")
    clock = Clock()
    adapter._idle_watcher = IdleWatcher(10.0, 2, clock)
    return adapter, clock


def test_the_adapter_speaks_the_nudge_and_then_ends_the_call():
    bridge = Bridge()
    metrics = CallMetrics("call-idle")
    metrics.bind()
    adapter, clock = adapter_with_bridge(bridge, metrics)

    async def scenario() -> None:
        for _ in range(int(60 / 0.02)):
            clock.advance(0.02)
            await adapter._observe_idle(False, False)

    asyncio.run(scenario())

    assert bridge.nudges == 2
    assert bridge.closed_for_silence is True
    summary = next(p for name, p in metrics.finish("completed") if name == "metrics_call")
    assert summary["idle_nudges"] == 2
    assert summary["idle_gave_up"] == 1


def test_no_nudge_once_the_call_is_closing():
    """Talking over the goodbye, or over the drain playing it out, is the
    exact failure the closing work has been about."""
    bridge = Bridge()
    adapter, clock = adapter_with_bridge(bridge)
    adapter._close_after_playback.set()

    async def scenario() -> None:
        for _ in range(int(60 / 0.02)):
            clock.advance(0.02)
            await adapter._observe_idle(False, False)

    asyncio.run(scenario())
    assert bridge.nudges == 0
    assert bridge.closed_for_silence is False


def test_a_talking_customer_is_left_alone_by_the_adapter():
    bridge = Bridge()
    adapter, clock = adapter_with_bridge(bridge)

    async def scenario() -> None:
        for index in range(int(120 / 0.02)):
            clock.advance(0.02)
            await adapter._observe_idle(index % 100 == 0, False)

    asyncio.run(scenario())
    assert bridge.nudges == 0
    assert bridge.closed_for_silence is False
