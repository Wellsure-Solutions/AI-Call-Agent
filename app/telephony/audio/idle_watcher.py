from __future__ import annotations

"""Decides when a silent line needs a nudge, and when to give up on it.

A customer who answers and then says nothing -- put the phone down, walked
away, a voicemail that answering-machine detection missed -- currently holds
the call until the maximum-call deadline. At the configured ceiling of one
concurrent call, that is the entire outbound queue stalled behind somebody
who is not there, plus the Twilio minutes to go with it.

The rules are deliberately simple, because every one of them is audible:

  * caller speech resets both the clock and the count. Somebody who answers a
    nudge is present, and must not accumulate strikes across a long call.
  * agent audio clears the clock. A customer listening is not a customer who
    has gone away, and after the agent stops they get a whole window to
    answer -- not the remainder of one.
  * a nudge restarts the clock, so the gap between nudges is the same as the
    gap before the first one.

This class only decides. Speaking the nudge and ending the call belong to the
caller, which is what keeps it testable against a clock that does not tick.
"""

from typing import Callable

# What the watcher is asking for.
NOTHING = ""
NUDGE = "nudge"
GIVE_UP = "give_up"


class IdleWatcher:
    def __init__(
        self,
        idle_seconds: float,
        max_nudges: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time

        self.idle_seconds = max(1.0, float(idle_seconds))
        self.max_nudges = max(0, int(max_nudges))
        self._clock = clock or time.monotonic
        self._quiet_since: float | None = None
        self.nudges = 0
        self.gave_up = False

    def observe(self, *, caller_voiced: bool, agent_playing: bool) -> str:
        """Feed one inbound frame's worth of state. Returns what to do."""
        if self.gave_up:
            return NOTHING

        now = self._clock()
        if caller_voiced:
            # They are here. Forget everything -- including earlier strikes,
            # because a customer who answers the second nudge should get the
            # same patience as one who never went quiet.
            self._quiet_since = None
            self.nudges = 0
            return NOTHING
        if agent_playing:
            # Listening is not idling, and the window restarts afterwards
            # rather than resuming. Carrying silence across the agent's turn
            # would nudge somebody two seconds after it stopped talking --
            # mid-thought, which is exactly when a real caller waits.
            self._quiet_since = None
            return NOTHING

        if self._quiet_since is None:
            self._quiet_since = now
            return NOTHING
        if now - self._quiet_since < self.idle_seconds:
            return NOTHING

        self._quiet_since = now
        if self.nudges >= self.max_nudges:
            self.gave_up = True
            return GIVE_UP
        self.nudges += 1
        return NUDGE

    @property
    def quiet_seconds(self) -> float:
        if self._quiet_since is None:
            return 0.0
        return self._clock() - self._quiet_since
