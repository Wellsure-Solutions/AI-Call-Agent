from __future__ import annotations

"""The settings rename that took production down, and the alarm for next time.

Reorganising this module dropped the CALL_AGENT_ prefix from most variables.
Nothing failed when it did -- a deployment whose .env still used the old
names silently got the defaults instead.

That is how CALL_AGENT_STREAM_SECRET stopped being read. STREAM_SECRET fell
back to "", `valid_stream_token` returns False whenever the secret is blank,
so every Twilio media stream was rejected and every outbound call dropped
about two seconds after the customer picked up. Twilio logged
"Connection reset without closing handshake"; our own logs showed a clean
200 on every webhook and an accepted websocket. Browser calls carry no media
token, so they kept working and hid it.
"""

import importlib

import pytest

from app.core import settings


@pytest.fixture(autouse=True)
def restore_settings():
    yield
    importlib.reload(settings)


def reloaded(monkeypatch, **env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return importlib.reload(settings)


# ---------------------------------------------------------------------------
# Both spellings, indefinitely
# ---------------------------------------------------------------------------
def test_the_old_env_name_still_configures_the_stream_secret(monkeypatch):
    """The exact variable whose silent disappearance dropped every call."""
    monkeypatch.delenv("STREAM_SECRET", raising=False)
    assert reloaded(monkeypatch, CALL_AGENT_STREAM_SECRET="legacy").STREAM_SECRET == "legacy"


def test_the_new_env_name_wins_when_both_are_present(monkeypatch):
    """A deployment mid-migration must not get the stale value."""
    current = reloaded(
        monkeypatch, STREAM_SECRET="current", CALL_AGENT_STREAM_SECRET="legacy"
    )
    assert current.STREAM_SECRET == "current"


@pytest.mark.parametrize(
    "legacy_name, attribute, value, expected",
    [
        ("CALL_AGENT_MAX_CONCURRENT_CALLS", "MAX_CONCURRENT_CALLS", "1", 1),
        ("CALL_AGENT_MAX_CALL_SECONDS", "MAX_CALL_SECONDS", "600", 600),
        ("CALL_AGENT_RING_TIMEOUT_SECONDS", "RING_TIMEOUT_SECONDS", "30", 30),
        ("CALL_AGENT_METRICS_ENABLED", "METRICS_ENABLED", "1", True),
        ("CALL_AGENT_AMD_ENABLED", "AMD_ENABLED", "true", True),
        ("CALL_AGENT_BARGE_IN_CONFIRM_MS", "BARGE_IN_CONFIRM_MS", "700", 700),
        ("CALL_AGENT_PLAYBACK_LEAD_MS", "PLAYBACK_LEAD_MS", "80", 80),
    ],
)
def test_every_renamed_setting_still_answers_to_its_old_name(
    monkeypatch, legacy_name, attribute, value, expected
):
    """Each of these would otherwise revert to a default without a word --
    the concurrency ceiling, the call deadline, whether metrics record at all.
    """
    monkeypatch.delenv(attribute, raising=False)
    assert getattr(reloaded(monkeypatch, **{legacy_name: value}), attribute) == expected


def test_a_rename_that_was_not_a_prefix_drop_is_aliased_too(monkeypatch):
    """The energy threshold gained a word as well as losing the prefix, so
    the general rule misses it."""
    monkeypatch.delenv("BARGE_IN_VOICE_ENERGY_THRESHOLD", raising=False)
    reloaded_settings = reloaded(monkeypatch, CALL_AGENT_BARGE_IN_ENERGY_THRESHOLD="333")
    assert reloaded_settings.BARGE_IN_VOICE_ENERGY_THRESHOLD == 333.0


def test_a_blank_value_counts_as_unset(monkeypatch):
    """An empty secret or an empty number is never what anybody meant, and a
    blank new-style entry must not shadow a real old-style one."""
    reloaded_settings = reloaded(
        monkeypatch, STREAM_SECRET="   ", CALL_AGENT_STREAM_SECRET="legacy"
    )
    assert reloaded_settings.STREAM_SECRET == "legacy"


# ---------------------------------------------------------------------------
# Why it went unnoticed, and what now says so
# ---------------------------------------------------------------------------
def test_an_empty_secret_rejects_every_media_stream():
    """Pinned because it is the mechanism, and it is not obvious: the token
    check fails closed, so a blank secret is indistinguishable from an
    attacker to every legitimate call."""
    from app.telephony import twilio_routes

    original = twilio_routes.STREAM_SECRET
    twilio_routes.STREAM_SECRET = ""
    try:
        assert twilio_routes.valid_stream_token("call", "CA1", 2 ** 31, "") is False
        assert twilio_routes.valid_stream_token("call", "CA1", 2 ** 31, "anything") is False
    finally:
        twilio_routes.STREAM_SECRET = original


def test_startup_names_the_settings_that_would_break_calls(monkeypatch):
    import app.main as main
    import app.core.settings as live

    monkeypatch.setattr(live, "STREAM_SECRET", "")
    problems = main.check_startup_configuration()
    assert any("STREAM_SECRET" in problem for problem in problems)
    assert any("drop seconds after being answered" in problem for problem in problems)


def test_a_correctly_configured_server_reports_nothing(monkeypatch):
    import app.main as main
    import app.core.settings as live

    for name in (
        "STREAM_SECRET", "DEEPGRAM_API_KEY", "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "PUBLIC_BASE_URL",
    ):
        monkeypatch.setattr(live, name, "set")
    assert main.check_startup_configuration() == []
