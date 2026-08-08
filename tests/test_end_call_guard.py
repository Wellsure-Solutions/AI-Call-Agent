from __future__ import annotations

"""The agent must not hang up without saying goodbye.

Reported twice from real calls: the customer says "अच्छा अच्छा, बिल्कुल बढ़िया
है" and the line goes dead. The provider log shows `end_call` arriving with no
assistant `ConversationText` between it and the customer's turn -- so every
downstream guard (AgentAudioDone, the playback drain) waited correctly for
audio that was never generated.

The prompt forbids this and the tool description says so twice. The model does
it anyway, which is why it is enforced in code here.
"""

import asyncio
import types

from app.core.conversation.conversation_engine import ConversationEngine
from app.core.settings import AGENT_CLOSE_GRACE_SECONDS, AGENT_CLOSE_UNSPOKEN_GRACE_SECONDS
from app.telephony.call_session import CallSession
from app.telephony.metrics import CallMetrics


class Harness:
    """A ConversationEngine with the Deepgram socket replaced by a list.

    Only the two things the guard actually decides are captured: what was sent
    back for the function call, and what grace the hangup was armed with.
    """

    def __init__(self, metrics: CallMetrics | None = None) -> None:
        self.session = CallSession(call_id="call-close", direction="twilio")
        self.finished = 0
        self.results: list[tuple[str, str]] = []
        self.graces: list[float] = []
        self.engine = ConversationEngine(
            self.session,
            on_audio=lambda _frame: None,
            on_text=lambda _payload: None,
            on_finished=self._on_finished,
            metrics=metrics,
        )
        self.engine._send_function_result = lambda call_id, name, content: self.results.append((name, content))

        async def record_grace(grace_seconds: float) -> None:
            self.graces.append(grace_seconds)

        self.engine._close_after_grace = record_grace

    def _on_finished(self) -> None:
        self.finished += 1

    def says(self, role: str, content: str) -> None:
        self.engine._on_message(types.SimpleNamespace(type="ConversationText", role=role, content=content))

    def requests_end_call(self, reason: str = "completed") -> None:
        self.engine._on_message(types.SimpleNamespace(
            type="FunctionCallRequest",
            functions=[types.SimpleNamespace(
                name="end_call", id="fc1", client_side=True, arguments='{"reason": "%s"}' % reason,
            )],
        ))

    @property
    def last_result(self) -> str:
        return self.results[-1][1] if self.results else ""


def run(scenario) -> Harness:
    """Drive a harness inside a running loop.

    The engine schedules its own work with `run_coroutine_threadsafe`, so it
    needs a live loop even though nothing here is concurrent.
    """
    async def main() -> Harness:
        harness = Harness()
        harness.engine.loop = asyncio.get_running_loop()
        scenario(harness)
        await asyncio.sleep(0)  # let anything scheduled on the loop run
        return harness

    return asyncio.run(main())


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------
def test_end_call_is_refused_when_nothing_was_said_since_the_customer_spoke():
    def scenario(harness: Harness) -> None:
        harness.says("user", "अच्छा अच्छा, बिल्कुल बढ़िया है")
        harness.requests_end_call()

    harness = run(scenario)
    assert "Say your closing line" in harness.last_result
    assert harness.engine._close_after_audio_done is False, "the hangup must not be armed"
    assert harness.engine.closing_requested is False
    assert harness.finished == 0, "the call must still be live"


def test_the_hangup_goes_through_once_the_closing_has_been_spoken():
    def scenario(harness: Harness) -> None:
        harness.says("user", "अच्छा अच्छा, बिल्कुल बढ़िया है")
        harness.requests_end_call()
        harness.says("assistant", "ठीक है सर, हमारी team आपको कॉल करेगी। नमस्ते!")
        harness.requests_end_call()

    harness = run(scenario)
    assert harness.last_result == "Call ending."
    assert harness.engine._close_after_audio_done is True


def test_a_closing_spoken_before_the_request_is_never_refused():
    """The normal, correct path: speak, then hang up. It must cost nothing."""
    def scenario(harness: Harness) -> None:
        harness.says("user", "ठीक है")
        harness.says("assistant", "धन्यवाद जी, आपका दिन शुभ हो!")
        harness.requests_end_call()

    harness = run(scenario)
    assert harness.results == [("end_call", "Call ending.")]
    assert harness.engine._close_after_audio_done is True


# ---------------------------------------------------------------------------
# A refusal must never become a call that cannot end
# ---------------------------------------------------------------------------
def test_a_second_request_is_obeyed_even_if_the_model_still_says_nothing():
    """A loop of refusals is worse than a missing goodbye: the customer would
    be held on a line that can never hang up."""
    def scenario(harness: Harness) -> None:
        harness.says("user", "अच्छा")
        harness.requests_end_call()
        harness.requests_end_call()

    harness = run(scenario)
    assert harness.last_result == "Call ending."
    assert harness.engine._close_after_audio_done is True


def test_the_refusal_arms_a_deadline_for_the_model_to_start_speaking():
    """It only has to cover starting a turn, not finishing one.

    The deadline fires on whether the model has *begun* a closing, which shows
    up as assistant text long before the audio ends -- and if it has not
    begun, we speak the goodbye ourselves rather than wait longer. A grace
    long enough for a whole turn would just be that many extra seconds of
    silence on a call nobody is going to close.
    """
    def scenario(harness: Harness) -> None:
        harness.says("user", "अच्छा")
        harness.requests_end_call()

    harness = run(scenario)
    assert harness.graces == [AGENT_CLOSE_UNSPOKEN_GRACE_SECONDS]
    assert AGENT_CLOSE_UNSPOKEN_GRACE_SECONDS <= AGENT_CLOSE_GRACE_SECONDS, (
        "waiting longer than the ordinary post-goodbye grace is dead air"
    )


def test_the_real_hangup_replaces_the_refusal_deadline():
    """Left running, the refusal's longer deadline would still be pending when
    the goodbye finishes, and the next arming would be silently skipped."""
    def scenario(harness: Harness) -> None:
        harness.says("user", "अच्छा")
        harness.requests_end_call()
        harness.says("assistant", "धन्यवाद जी, नमस्ते")
        harness.requests_end_call()

    harness = run(scenario)
    assert harness.graces == [AGENT_CLOSE_UNSPOKEN_GRACE_SECONDS, AGENT_CLOSE_GRACE_SECONDS]


def test_the_close_deadline_still_fires_when_no_provider_event_arrives():
    """The guard is allowed to delay a hangup, never to prevent one."""
    async def main() -> tuple[bool, int]:
        session = CallSession(call_id="call-deadline", direction="twilio")
        finished = 0

        def on_finished() -> None:
            nonlocal finished
            finished += 1

        engine = ConversationEngine(
            session, lambda _f: None, lambda _t: None, on_finished,
        )
        engine.loop = asyncio.get_running_loop()
        await engine._close_after_grace(0.01)
        return engine.closing_requested, finished

    closing_requested, finished = asyncio.run(main())
    assert closing_requested is True
    assert finished == 1


# ---------------------------------------------------------------------------
# Cases where hanging up straight away is correct
# ---------------------------------------------------------------------------
def test_end_call_before_the_customer_has_ever_spoken_is_honoured():
    """Voicemail, or a line nobody talks on. Nothing is owed to a customer who
    has not taken a turn, and stalling here burns a concurrency slot."""
    def scenario(harness: Harness) -> None:
        harness.requests_end_call("voicemail")

    harness = run(scenario)
    assert harness.last_result == "Call ending."


def test_a_greeting_the_agent_spoke_counts_as_having_spoken():
    def scenario(harness: Harness) -> None:
        harness.says("assistant", "नमस्ते सर, मैं Shruti बोल रही हूँ")
        harness.requests_end_call("voicemail")

    harness = run(scenario)
    assert harness.last_result == "Call ending."


def test_assistant_text_that_sanitizes_to_nothing_does_not_count_as_spoken():
    """A turn that was entirely a leaked internal command is stripped before
    synthesis, so the customer heard silence -- it cannot satisfy the guard."""
    def scenario(harness: Harness) -> None:
        harness.says("user", "अच्छा")
        harness.says("assistant", "_closing_call()")
        harness.requests_end_call()

    harness = run(scenario)
    assert "Say your closing line" in harness.last_result


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------
def test_a_refusal_is_counted_so_the_rate_is_visible():
    """A rising rate here means the prompt has stopped carrying the closing
    rule, which is otherwise only observable by listening to calls."""
    async def main() -> CallMetrics:
        metrics = CallMetrics("call-metrics")
        metrics.bind()
        harness = Harness(metrics=metrics)
        harness.engine.loop = asyncio.get_running_loop()
        harness.says("user", "अच्छा")
        harness.requests_end_call()
        await asyncio.sleep(0)
        return metrics

    metrics = asyncio.run(main())
    events = metrics.finish("test")
    names = [name for name, _payload in events]
    assert "metrics_end_call_refused" in names
    summary = next(payload for name, payload in events if name == "metrics_call")
    assert summary["end_call_refusals"] == 1
