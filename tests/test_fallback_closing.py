from __future__ import annotations

"""When the model will not say goodbye, we say it.

From a real call: the customer said "ठीक है." and the line went dead with no
closing at all. The engine already refuses the first such hangup and asks the
model for a closing. This is what happens when it still says nothing -- a
customer is never dropped in silence.

The goodbye is pre-rendered through the same voice as the rest of the call,
so a customer who hears it does not hear the agent change person on the last
sentence.
"""

import asyncio

import pytest

from app.core import settings
from app.core.conversation import conversation_engine as engine_module
from app.core.conversation.conversation_engine import ConversationEngine
from app.telephony.audio.greeting_cache import (
    cache_path,
    greeting_fingerprint,
    load_greeting,
    save_greeting,
)
from app.telephony.call_session import CallSession
from app.telephony.metrics import CallMetrics


CLOSING_AUDIO = b"\x7f" * 24000  # three seconds of 8 kHz mu-law


def engine_for(direction: str, metrics: CallMetrics | None = None):
    played: list[bytes] = []
    session = CallSession(call_id=f"call-{direction}", direction=direction)
    engine = ConversationEngine(
        session,
        on_audio=played.append,
        on_text=lambda _payload: None,
        on_finished=lambda: None,
        metrics=metrics,
    )
    return engine, session, played


@pytest.fixture
def rendered(monkeypatch):
    monkeypatch.setattr(engine_module, "cached_closing_audio", lambda: CLOSING_AUDIO)
    return CLOSING_AUDIO


# ---------------------------------------------------------------------------
# The reported failure
# ---------------------------------------------------------------------------
def test_a_call_the_model_abandons_still_gets_a_spoken_goodbye(rendered):
    """The whole point: the line never goes quiet on a customer."""
    engine, session, played = engine_for("twilio")
    engine._assistant_spoke_since_user = False

    asyncio.run(engine._close_after_grace(0.01))

    assert played == [rendered]
    assert session.transcript_text.strip().endswith(settings.DEEPGRAM_FALLBACK_CLOSING)


def test_the_goodbye_is_queued_before_the_call_is_finished(rendered):
    """Ordering is the whole mechanism. The adapter drains the outbound queue
    in order, so audio queued before the close signal is played; audio queued
    after it is never reached."""
    order: list[str] = []
    session = CallSession(call_id="call-order", direction="twilio")
    engine = ConversationEngine(
        session,
        on_audio=lambda _frame: order.append("audio"),
        on_text=lambda _payload: None,
        on_finished=lambda: order.append("finished"),
    )
    engine._assistant_spoke_since_user = False

    asyncio.run(engine._close_after_grace(0.01))

    assert order == ["audio", "finished"]


def test_a_model_that_did_speak_is_left_alone(rendered):
    """Otherwise the customer hears two goodbyes, the second in a different
    sentence from the first."""
    engine, _session, played = engine_for("twilio")
    engine._assistant_spoke_since_user = True

    asyncio.run(engine._close_after_grace(0.01))

    assert played == []


def test_nothing_is_spoken_once_the_call_is_already_closing(rendered):
    engine, _session, played = engine_for("twilio")
    engine._assistant_spoke_since_user = False
    engine.closing_requested = True

    asyncio.run(engine._close_after_grace(0.01))

    assert played == []


# ---------------------------------------------------------------------------
# Where it must not play
# ---------------------------------------------------------------------------
def test_a_browser_session_never_gets_the_phone_audio(rendered):
    """The cache is 8 kHz mu-law because that is what the phone leg carries.
    A browser session runs linear16 at 24 kHz -- pushing these bytes into it
    emits noise, not a goodbye."""
    engine, _session, played = engine_for("browser")
    engine._assistant_spoke_since_user = False

    asyncio.run(engine._close_after_grace(0.01))

    assert played == []


def test_an_unrendered_cache_closes_the_call_exactly_as_before(monkeypatch):
    """A missing cache must never be the reason a call fails. It costs the
    goodbye, not the call."""
    monkeypatch.setattr(engine_module, "cached_closing_audio", lambda: None)
    engine, _session, played = engine_for("twilio")
    engine._assistant_spoke_since_user = False

    asyncio.run(engine._close_after_grace(0.01))

    assert played == []
    assert engine.closing_requested is True


# ---------------------------------------------------------------------------
# The cache itself
# ---------------------------------------------------------------------------
def test_the_closing_and_the_greeting_do_not_share_a_file(tmp_path):
    """They are rendered from the same voice and the same config; only the
    text differs. Colliding would play the greeting as the goodbye."""
    voice = ("eleven_labs", "eleven_flash_v2_5", "voice-a", "hi")
    greeting = greeting_fingerprint("नमस्ते", *voice)
    closing = greeting_fingerprint("धन्यवाद", *voice)
    assert greeting != closing
    assert cache_path(tmp_path, greeting) != cache_path(tmp_path, closing, "closing")


def test_a_closing_round_trips_under_its_own_kind(tmp_path):
    save_greeting(tmp_path, "abc", CLOSING_AUDIO, "closing")
    assert load_greeting(tmp_path, "abc", "closing") == CLOSING_AUDIO
    assert load_greeting(tmp_path, "abc") is None, "must not be readable as a greeting"


def test_the_configured_closing_is_outcome_neutral():
    """It is spoken without knowing how the call went, so it has to be true
    after a yes, a no, and a wrong number alike."""
    closing = settings.DEEPGRAM_FALLBACK_CLOSING
    assert closing
    for presumptuous in ("team", "call करेगी", "switch", "account"):
        assert presumptuous not in closing, f"{presumptuous!r} assumes the call went well"


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------
def test_speaking_for_the_model_is_counted_separately_from_refusing(rendered):
    """A refusal the model then answers is the guard working. This is the
    guard being the only thing that spoke, and the two must not be confused
    in the batch report."""
    metrics = CallMetrics("call-fallback")
    metrics.bind()
    engine, _session, _played = engine_for("twilio", metrics=metrics)
    engine._assistant_spoke_since_user = False

    asyncio.run(engine._close_after_grace(0.01))

    summary = next(p for name, p in metrics.finish("completed") if name == "metrics_call")
    assert summary["fallback_closings"] == 1
    assert summary["end_call_refusals"] == 0
