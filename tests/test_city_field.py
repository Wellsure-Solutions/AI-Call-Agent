"""City is a real column on leads and calls, not just a UI label.

Covers the v4 migration (fresh DB and an upgraded legacy DB), that city
flows from lead import through to a call row at enqueue, and that the
export filters (search/status/category/interested/city/date range) that
back the dashboard's "export what I'm filtering on" behavior actually
narrow the result set.
"""

import json

import pytest

from app.storage.sqlite_store import SQLiteCallStore

try:
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover
    load_workbook = None


@pytest.fixture()
def store(tmp_path) -> SQLiteCallStore:
    return SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)


def make_lead(store: SQLiteCallStore, **overrides) -> dict:
    row = {
        "business_name": "Acme",
        "phone_number": "+14155550001",
        "category": "Retail",
        "city": "Mumbai",
        "notes": "",
    }
    row.update(overrides)
    return store.import_leads([row])["leads"][0]


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------
def test_fresh_database_has_city_on_leads_and_calls(store):
    with store.transaction() as db:
        lead_columns = {row[1] for row in db.execute("PRAGMA table_info(leads)")}
        call_columns = {row[1] for row in db.execute("PRAGMA table_info(calls)")}
    assert "city" in lead_columns
    assert "city" in call_columns


def test_legacy_database_without_city_is_migrated_on_reopen(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store = SQLiteCallStore(path, tmp_path)
    with store.transaction(immediate=True) as db:
        db.execute("DROP INDEX IF EXISTS leads_city")
        db.execute("DROP INDEX IF EXISTS calls_city")
        db.execute("ALTER TABLE leads DROP COLUMN city")
        db.execute("ALTER TABLE calls DROP COLUMN city")
        db.execute("UPDATE schema_metadata SET value='3' WHERE key='schema_version'")

    reopened = SQLiteCallStore(path, tmp_path)
    with reopened.transaction() as db:
        lead_columns = {row[1] for row in db.execute("PRAGMA table_info(leads)")}
        version = db.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0]
    assert "city" in lead_columns
    # v5 (inbound_calls) chains straight off v4, so a reopened database lands there.
    assert version == "5"
    # And the migration didn't just add the column -- normal operations work.
    lead = make_lead(reopened)
    assert lead["city"] == "Mumbai"


def test_migration_is_idempotent_across_repeated_startup(tmp_path):
    path = tmp_path / "repeat.sqlite3"
    for _ in range(3):
        SQLiteCallStore(path, tmp_path)
    store = SQLiteCallStore(path, tmp_path)
    with store.transaction() as db:
        version = db.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0]
    assert version == "5"


# ---------------------------------------------------------------------------
# City flows from a lead through to its calls
# ---------------------------------------------------------------------------
def test_import_leads_stores_city(store):
    lead = make_lead(store, city="Pune")
    assert lead["city"] == "Pune"
    assert store.get_lead(lead["lead_id"])["city"] == "Pune"


def test_enqueue_call_writes_city_onto_the_call_row(store):
    lead = make_lead(store, city="Delhi")
    call = store.enqueue_call(phone_number=lead["phone_number"], lead_id=lead["lead_id"], city=lead["city"])
    assert call["city"] == "Delhi"
    assert store.get_call(call["call_id"])["city"] == "Delhi"


def test_enqueue_call_without_city_defaults_to_empty(store):
    call = store.enqueue_call(phone_number="+14155550009")
    assert call["city"] == ""


def test_update_lead_can_change_city(store):
    lead = make_lead(store, city="Pune")
    updated = store.update_lead(lead["lead_id"], city="Nagpur")
    assert updated["city"] == "Nagpur"


# ---------------------------------------------------------------------------
# Export filtering: the fix for "the excel downloads the whole dataset"
# ---------------------------------------------------------------------------
def _enqueue(store, phone, *, business_name, category, city, interested=False, outcome="completed"):
    lead = make_lead(store, business_name=business_name, phone_number=phone, category=category, city=city)
    call = store.enqueue_call(phone_number=phone, lead_id=lead["lead_id"], business_name=business_name, category=category, city=city)
    with store.transaction(immediate=True) as db:
        db.execute(
            "UPDATE calls SET lifecycle_state='COMPLETED',outcome=?,interested=?,created_at=?,started_at=?,ended_at=? WHERE call_id=?",
            (outcome, 1 if interested else 0, "2024-01-15T10:00:00+00:00", "2024-01-15T10:00:00+00:00", "2024-01-15T10:05:00+00:00", call["call_id"]),
        )
    return call["call_id"]


def test_iter_calls_city_filter_narrows_results(store):
    _enqueue(store, "+14155550101", business_name="Mumbai Co", category="Retail", city="Mumbai")
    _enqueue(store, "+14155550102", business_name="Delhi Co", category="Retail", city="Delhi")

    mumbai_only = list(store.iter_calls(city="Mumbai"))

    assert [c["business_name"] for c in mumbai_only] == ["Mumbai Co"]


def test_iter_calls_search_matches_city_too(store):
    _enqueue(store, "+14155550201", business_name="Alpha", category="Retail", city="Bengaluru")
    _enqueue(store, "+14155550202", business_name="Beta", category="Retail", city="Chennai")

    matched = list(store.iter_calls(search="bengaluru"))

    assert [c["business_name"] for c in matched] == ["Alpha"]


def test_iter_calls_status_and_interested_filters_combine(store):
    _enqueue(store, "+14155550301", business_name="Won", category="Retail", city="Pune", interested=True, outcome="completed")
    _enqueue(store, "+14155550302", business_name="Lost", category="Retail", city="Pune", interested=False, outcome="completed")
    _enqueue(store, "+14155550303", business_name="NoAnswer", category="Retail", city="Pune", interested=False, outcome="no_answer")

    won_only = list(store.iter_calls(status="completed", interested="true"))

    assert [c["business_name"] for c in won_only] == ["Won"]


def test_iter_calls_date_range_is_inclusive_by_day(store):
    call_id = _enqueue(store, "+14155550401", business_name="DatedCo", category="Retail", city="Pune")

    assert [c["call_id"] for c in store.iter_calls(date_from="2024-01-15", date_to="2024-01-15")] == [call_id]
    assert list(store.iter_calls(date_from="2024-01-16")) == []
    assert list(store.iter_calls(date_to="2024-01-14")) == []


def test_export_calls_json_respects_city_filter(store):
    _enqueue(store, "+14155550501", business_name="Mumbai Co", category="Retail", city="Mumbai")
    _enqueue(store, "+14155550502", business_name="Delhi Co", category="Retail", city="Delhi")

    _, content, _ = store.export_calls("json", city="Delhi")
    rows = json.loads(content)

    assert [row["business_name"] for row in rows] == ["Delhi Co"]


def test_export_calls_csv_header_includes_city(store):
    _enqueue(store, "+14155550601", business_name="Mumbai Co", category="Retail", city="Mumbai")
    _, content, _ = store.export_calls("csv")
    header = content.decode().splitlines()[0]
    assert "city" in header


@pytest.mark.skipif(load_workbook is None, reason="openpyxl is unavailable")
def test_export_calls_unfiltered_still_returns_everything(store):
    _enqueue(store, "+14155550701", business_name="A", category="Retail", city="Mumbai")
    _enqueue(store, "+14155550702", business_name="B", category="Retail", city="Delhi")

    _, content, _ = store.export_calls("json")
    rows = json.loads(content)

    assert {row["business_name"] for row in rows} == {"A", "B"}


# ---------------------------------------------------------------------------
# Leads export mirrors the same filtering
# ---------------------------------------------------------------------------
def test_export_leads_json_respects_filters(store):
    make_lead(store, business_name="Mumbai Co", phone_number="+14155550801", category="Retail", city="Mumbai")
    make_lead(store, business_name="Delhi Co", phone_number="+14155550802", category="Wholesale", city="Delhi")

    _, content, _ = store.export_leads("json", city="Mumbai")
    rows = json.loads(content)

    assert [row["business_name"] for row in rows] == ["Mumbai Co"]


def test_export_leads_category_filter(store):
    make_lead(store, business_name="Mumbai Co", phone_number="+14155550901", category="Retail", city="Mumbai")
    make_lead(store, business_name="Delhi Co", phone_number="+14155550902", category="Wholesale", city="Delhi")

    _, content, _ = store.export_leads("json", category="Wholesale")
    rows = json.loads(content)

    assert [row["business_name"] for row in rows] == ["Delhi Co"]


def test_export_leads_csv_headers_are_human_readable(store):
    make_lead(store, business_name="Mumbai Co", phone_number="+14155551001", category="Retail", city="Mumbai")
    _, content, _ = store.export_leads("csv")
    header = content.decode().splitlines()[0]
    assert header == "Business Name,Phone Number,Category,City,Notes,Status,Added,Updated"


@pytest.mark.skipif(load_workbook is None, reason="openpyxl is unavailable")
def test_export_leads_xlsx_round_trips(store):
    make_lead(store, business_name="Mumbai Co", phone_number="+14155551101", category="Retail", city="Mumbai")
    filename, content, media_type = store.export_leads("xlsx")

    assert filename == "leads.xlsx"
    assert media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    workbook = load_workbook(__import__("io").BytesIO(content))
    sheet = workbook.active
    assert [cell.value for cell in sheet[1]][:4] == ["Business Name", "Phone Number", "Category", "City"]
    assert sheet["D2"].value == "Mumbai"


@pytest.mark.skipif(load_workbook is None, reason="openpyxl is unavailable")
def test_lead_template_includes_city_column(store):
    filename, content, _ = store.export_lead_template()
    workbook = load_workbook(__import__("io").BytesIO(content))
    sheet = workbook.active
    assert filename == "lead-upload-template.xlsx"
    assert [cell.value for cell in sheet[1]] == ["Business Name", "Phone Number", "Category", "City", "Notes"]
    # Column B (phone) must still format as text -- unrelated regression check
    # for the column this test file shares with test_json_store.py.
    assert sheet["B1"].value == "Phone Number"
