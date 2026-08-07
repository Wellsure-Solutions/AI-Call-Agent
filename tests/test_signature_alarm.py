from __future__ import annotations

import pytest

from app.telephony import twilio_routes


@pytest.fixture(autouse=True)
def reset_counters():
    twilio_routes._signature_failures.update(total=0, by_endpoint={}, last_at=None, last_endpoint=None)
    yield


def test_no_failures_reports_a_clean_state():
    health = twilio_routes.signature_failure_health()
    assert health["total"] == 0
    assert health["by_endpoint"] == {}
    assert health["last_at"] is None


def test_rejections_are_counted_per_endpoint():
    """A wrong PUBLIC_BASE_URL rejects every callback, so which endpoints are
    failing tells an operator whether it is a config problem (all of them) or
    something narrower."""
    twilio_routes._record_signature_failure("twiml", "call-1")
    twilio_routes._record_signature_failure("status", "call-1")
    twilio_routes._record_signature_failure("status", "call-2")

    health = twilio_routes.signature_failure_health()
    assert health["total"] == 3
    assert health["by_endpoint"] == {"twiml": 1, "status": 2}
    assert health["last_endpoint"] == "status"
    assert health["last_at"] is not None


def test_health_snapshot_cannot_be_mutated_by_a_caller():
    """The endpoint serialises this straight into a response; handing out the
    live dict would let a reader corrupt the counters."""
    snapshot = twilio_routes.signature_failure_health()
    snapshot["by_endpoint"]["injected"] = 99
    snapshot["total"] = 1000

    assert twilio_routes.signature_failure_health()["total"] == 0
    assert "injected" not in twilio_routes.signature_failure_health()["by_endpoint"]


def test_the_alarm_never_logs_the_auth_token(caplog):
    """The configured public origin is the useful diagnostic; the token that
    signs against it is not, and must not reach logs."""
    import logging

    with caplog.at_level(logging.WARNING):
        twilio_routes._record_signature_failure("status", "call-3")

    record = next(r for r in caplog.records if r.msg == "twilio_signature_rejected")
    assert hasattr(record, "configured_public_base_url")
    assert not any("token" in key.lower() or "secret" in key.lower() for key in record.__dict__)
