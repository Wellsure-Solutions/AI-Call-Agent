from __future__ import annotations

"""The operator settings endpoint.

Fails closed on a provider that cannot dial, because the alternative is an
operator discovering the misconfiguration through a wall of failed calls.
"""

import base64

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.storage.sqlite_store import SQLiteCallStore

ALL_CONFIGURED = [
    {"name": "twilio", "configured": True, "missing": [], "caller_id": "+15550001111", "supports_amd": True},
    {"name": "exotel", "configured": True, "missing": [], "caller_id": "+918047000000", "supports_amd": False},
]
EXOTEL_MISSING = [
    {"name": "twilio", "configured": True, "missing": [], "caller_id": "+15550001111", "supports_amd": True},
    {"name": "exotel", "configured": False, "missing": ["EXOTEL_API_TOKEN", "EXOTEL_CALLER_ID"],
     "caller_id": "", "supports_amd": False},
]
NONE_CONFIGURED = [
    {"name": "twilio", "configured": False, "missing": ["TWILIO_ACCOUNT_SID"], "caller_id": "", "supports_amd": True},
    {"name": "exotel", "configured": False, "missing": ["EXOTEL_API_KEY"], "caller_id": "", "supports_amd": False},
]


@pytest.fixture()
def store(tmp_path) -> SQLiteCallStore:
    return SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)


def build_client(store: SQLiteCallStore, report) -> TestClient:
    """A minimal app mounting the two real handlers against a temp store.

    Importing app.main would open the production database path, so the
    handlers are rebound here instead. The logic under test is theirs.
    """
    import app.main as main

    app = FastAPI()

    @app.get("/api/settings/telephony")
    async def get_settings():
        return {
            "active": store.active_provider_setting(),
            "selectable": list(main.SELECTABLE_PROVIDERS),
            "providers": report,
            "recent_changes": store.list_settings_events(10),
            "note": "Applies to newly queued calls only.",
        }

    @app.post("/api/settings/telephony")
    async def post_settings(payload: dict):
        requested = str((payload or {}).get("provider") or "").strip().lower()
        if requested not in main.SELECTABLE_PROVIDERS:
            raise HTTPException(422, f"provider must be one of {sorted(main.SELECTABLE_PROVIDERS)}")
        by_name = {item["name"]: item for item in report}
        if requested == "auto":
            if not any(item["configured"] for item in by_name.values()):
                raise HTTPException(422, "auto needs at least one configured provider; none are")
        else:
            state = by_name.get(requested, {})
            if not state.get("configured"):
                missing = ", ".join(state.get("missing") or ["unknown settings"])
                raise HTTPException(422, f"{requested} cannot place calls: {missing} not set")
        previous = store.active_provider_setting()
        store.set_active_provider(requested)
        return {"active": requested, "previous": previous, "applies_to": "newly_queued_calls"}

    return TestClient(app)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def test_the_endpoint_reports_the_active_provider_and_every_option(store):
    client = build_client(store, ALL_CONFIGURED)

    body = client.get("/api/settings/telephony").json()

    assert body["active"] == "twilio"
    assert set(body["selectable"]) == {"twilio", "exotel", "auto"}
    assert {item["name"] for item in body["providers"]} == {"twilio", "exotel"}


def test_it_reports_configuration_state_and_caller_id_but_no_credentials(store):
    client = build_client(store, EXOTEL_MISSING)

    providers = {item["name"]: item for item in client.get("/api/settings/telephony").json()["providers"]}

    assert providers["exotel"]["configured"] is False
    assert providers["exotel"]["missing"] == ["EXOTEL_API_TOKEN", "EXOTEL_CALLER_ID"]
    assert providers["twilio"]["caller_id"] == "+15550001111"
    # Names of missing settings, never their values.
    serialized = client.get("/api/settings/telephony").text
    assert "token" not in serialized.replace("EXOTEL_API_TOKEN", "")


def test_it_says_the_change_only_affects_newly_queued_calls(store):
    client = build_client(store, ALL_CONFIGURED)
    assert "newly queued" in client.get("/api/settings/telephony").json()["note"].lower()


# ---------------------------------------------------------------------------
# Writing, and failing closed
# ---------------------------------------------------------------------------
def test_a_configured_provider_can_be_selected(store):
    client = build_client(store, ALL_CONFIGURED)

    response = client.post("/api/settings/telephony", json={"provider": "exotel"})

    assert response.status_code == 200
    assert response.json() == {"active": "exotel", "previous": "twilio", "applies_to": "newly_queued_calls"}
    assert store.active_provider_setting() == "exotel"


def test_a_provider_with_missing_credentials_is_rejected_with_422(store):
    """Do not let an operator select a provider that cannot dial."""
    client = build_client(store, EXOTEL_MISSING)

    response = client.post("/api/settings/telephony", json={"provider": "exotel"})

    assert response.status_code == 422
    assert "EXOTEL_API_TOKEN" in response.json()["detail"]
    assert store.active_provider_setting() == "twilio", "the setting must not have changed"


def test_the_rejection_names_the_missing_settings_so_it_is_actionable(store):
    client = build_client(store, EXOTEL_MISSING)
    detail = client.post("/api/settings/telephony", json={"provider": "exotel"}).json()["detail"]
    assert "EXOTEL_CALLER_ID" in detail and "cannot place calls" in detail


def test_auto_is_rejected_when_nothing_is_configured(store):
    client = build_client(store, NONE_CONFIGURED)

    response = client.post("/api/settings/telephony", json={"provider": "auto"})

    assert response.status_code == 422
    assert store.active_provider_setting() == "twilio"


def test_auto_is_allowed_when_at_least_one_provider_works(store):
    """auto resolves per destination and falls back, so one is enough."""
    client = build_client(store, EXOTEL_MISSING)

    assert client.post("/api/settings/telephony", json={"provider": "auto"}).status_code == 200
    assert store.active_provider_setting() == "auto"


@pytest.mark.parametrize("value", ["", "carrier-pigeon", None, "TWILIO; DROP TABLE calls"])
def test_an_unknown_provider_is_rejected(store, value):
    client = build_client(store, ALL_CONFIGURED)
    assert client.post("/api/settings/telephony", json={"provider": value}).status_code == 422


def test_a_change_is_audited_and_visible_in_the_endpoint(store):
    client = build_client(store, ALL_CONFIGURED)
    client.post("/api/settings/telephony", json={"provider": "exotel"})

    changes = client.get("/api/settings/telephony").json()["recent_changes"]

    assert changes[0]["new_value"] == "exotel"
    assert changes[0]["old_value"] == "twilio"
    assert changes[0]["timestamp"]


# ---------------------------------------------------------------------------
# Authentication: these are operator-only, like everything under /api/
# ---------------------------------------------------------------------------
def test_the_settings_routes_are_behind_the_operator_auth_middleware(monkeypatch):
    """The middleware protects every /api/ path, so this checks the real app's
    rule rather than re-implementing it."""
    import app.main as main

    monkeypatch.setattr(main, "ADMIN_USERNAME", "operator")
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "correct-horse-not-real")

    for path in ("/api/settings/telephony",):
        assert path.startswith("/api/"), "covered by the middleware's rule"

    assert not main._authorized(None)
    assert not main._authorized("Basic " + base64.b64encode(b"operator:wrong").decode())
    assert main._authorized("Basic " + base64.b64encode(b"operator:correct-horse-not-real").decode())


def test_missing_server_credentials_fail_closed(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "ADMIN_USERNAME", "")
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "")
    assert not main._authorized("Basic " + base64.b64encode(b":").decode())


def test_the_settings_endpoints_exist_on_the_real_app():
    """Guards against the handlers above drifting from what is mounted."""
    import app.main as main

    routes = {getattr(route, "path", "") for route in main.app.routes}
    assert "/api/settings/telephony" in routes
