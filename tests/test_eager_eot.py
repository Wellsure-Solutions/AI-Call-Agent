from __future__ import annotations

"""Eager end-of-turn: the settings it produces, and the cost it records.

Flux normally waits until it is confident the customer has stopped. Eager
end-of-turn acts on moderate confidence instead, so the reply is drafted
150-250ms earlier -- and thrown away when the customer turns out not to have
finished. That discard is the price, and it is also the naturalness risk:
firing inside a pause is the agent answering over someone.
"""

import importlib
import types

import pytest

from app.core import settings
from app.integrations.deepgram.config import get_agent_settings
from app.telephony.metrics import CallMetrics


def listen_provider(settings_object) -> dict:
    return settings_object.dict()["agent"]["listen"]["provider"]


# ---------------------------------------------------------------------------
# What actually reaches Deepgram
# ---------------------------------------------------------------------------
def test_the_eager_threshold_is_sent_on_the_settings_message():
    provider = listen_provider(get_agent_settings(None, "twilio"))
    assert provider["eager_eot_threshold"] == settings.DEEPGRAM_EAGER_EOT_THRESHOLD


def test_it_is_enabled_by_default():
    """The point of the change. Left unset, Deepgram disables eager mode."""
    assert settings.DEEPGRAM_EAGER_EOT_THRESHOLD is not None


def test_the_default_sits_at_the_fewest_false_starts_end_of_the_useful_band():
    """Deepgram documents 0.3-0.5 as the useful range, earlier at the bottom
    and more false starts with it. A false start is the failure this project
    exists to fix, so the default starts at the top."""
    assert settings.DEEPGRAM_EAGER_EOT_THRESHOLD == 0.5


def test_flux_still_gets_its_language_hints_and_final_threshold():
    """flux-general-multi requires language_hints, and eager mode does not
    replace the final end-of-turn decision -- it runs ahead of it."""
    provider = listen_provider(get_agent_settings(None, "twilio"))
    assert provider["model"] == "flux-general-multi"
    assert provider["language_hints"] == ["hi", "en"]
    assert provider["eot_threshold"] == settings.DEEPGRAM_EOT_THRESHOLD


def test_the_browser_path_gets_the_same_turn_taking_config():
    """Otherwise browser test calls stop predicting what a phone call does."""
    assert listen_provider(get_agent_settings(None, "browser")) == listen_provider(
        get_agent_settings(None, "twilio")
    )


# ---------------------------------------------------------------------------
# Refusing a value Deepgram would reject
# ---------------------------------------------------------------------------
def reload_settings_with(monkeypatch, **env: str):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return importlib.reload(settings)


@pytest.fixture(autouse=True)
def restore_settings():
    yield
    importlib.reload(settings)


def test_a_threshold_outside_deepgrams_range_fails_at_import(monkeypatch):
    """Deepgram rejects the whole Settings message, and a rejected Settings
    message is a call that connects and never speaks. That is the hardest
    symptom there is to diagnose from the outside, so it fails here instead --
    where the traceback names the variable."""
    for bad in ("0.1", "0.95", "2"):
        with pytest.raises(ValueError, match="DEEPGRAM_EAGER_EOT_THRESHOLD"):
            reload_settings_with(monkeypatch, DEEPGRAM_EAGER_EOT_THRESHOLD=bad)


def test_an_eager_threshold_above_the_final_one_is_refused(monkeypatch):
    """Eager fires at lower confidence than the final decision. Above it, the
    eager signal could only arrive once the turn was already final -- paying
    for the extra LLM calls and buying no latency at all."""
    with pytest.raises(ValueError, match="must not exceed"):
        reload_settings_with(
            monkeypatch, DEEPGRAM_EAGER_EOT_THRESHOLD="0.8", DEEPGRAM_EOT_THRESHOLD="0.7"
        )


def test_an_empty_value_turns_it_back_off(monkeypatch):
    """The rollback path, and the way to produce a baseline batch."""
    reloaded = reload_settings_with(monkeypatch, DEEPGRAM_EAGER_EOT_THRESHOLD="")
    assert reloaded.DEEPGRAM_EAGER_EOT_THRESHOLD is None


def test_the_key_is_omitted_entirely_when_disabled(monkeypatch):
    """Sending a null would be a different request from not asking for eager
    mode, and the provider is entitled to reject it."""
    reload_settings_with(monkeypatch, DEEPGRAM_EAGER_EOT_THRESHOLD="")
    import app.integrations.deepgram.config as config
    importlib.reload(config)
    try:
        assert "eager_eot_threshold" not in listen_provider(config.get_agent_settings(None, "twilio"))
    finally:
        importlib.reload(settings)
        importlib.reload(config)


# ---------------------------------------------------------------------------
# Measuring what it costs
# ---------------------------------------------------------------------------
def test_the_setting_travels_with_the_measurements():
    """Comparing a batch with eager mode against one without is the only way
    to judge it, and neither batch says which it was unless the config is
    recorded alongside the numbers."""
    metrics = CallMetrics("call-eager", eager_eot=0.5)
    metrics.bind()
    events = dict(metrics.drain())
    assert events["metrics_bound"]["eager_eot"] == 0.5


def test_discarded_drafts_are_counted():
    """A resume is a draft thrown away: the wasted LLM call this feature
    trades for latency, and the signal that the threshold is too low."""
    metrics = CallMetrics("call-eager", eager_eot=0.5)
    metrics.bind()
    for _ in range(4):
        metrics.eager_turn_started()
    metrics.turn_resumed()

    summary = next(p for name, p in metrics.finish("completed") if name == "metrics_call")
    assert summary["eager_turns"] == 4
    assert summary["turn_resumes"] == 1
    assert summary["eager_eot"] == 0.5


def test_provider_turn_events_reach_the_counters():
    """The engine must recognise the event names Flux uses, or the cost side
    of this feature is invisible."""
    from app.core.conversation.conversation_engine import ConversationEngine
    from app.telephony.call_session import CallSession

    metrics = CallMetrics("call-eager", eager_eot=0.5)
    metrics.bind()
    engine = ConversationEngine(
        CallSession(call_id="call-eager", direction="twilio"),
        lambda _f: None, lambda _t: None, lambda: None, metrics=metrics,
    )
    engine._on_message(types.SimpleNamespace(type="EagerEndOfTurn"))
    engine._on_message(types.SimpleNamespace(type="TurnResumed"))

    summary = next(p for name, p in metrics.finish("completed") if name == "metrics_call")
    assert summary["eager_turns"] == 1
    assert summary["turn_resumes"] == 1
