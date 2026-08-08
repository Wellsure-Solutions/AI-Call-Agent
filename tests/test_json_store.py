from app.storage.json_store import JsonCallStore


def test_parse_pasted_tab_separated_leads_and_import_summary(tmp_path):
    store = JsonCallStore(tmp_path)
    content = (
        "Business Name\tPhone Number\tCategory\tNotes\n"
        "Sharma Electronics\t+91 98765 43210\tMobile Accessories\tEvening callback\n"
        "Bad Lead\t123\tRetail\tToo short\n"
    ).encode("utf-8")

    headers, rows = store.parse_upload(content, "pasted.csv")

    assert headers == ["Business Name", "Phone Number", "Category", "Notes"]
    result = store.import_leads(
        [
            {
                "business_name": row.get("Business Name"),
                "phone_number": row.get("Phone Number"),
                "category": row.get("Category"),
                "notes": row.get("Notes"),
            }
            for row in rows
        ]
    )

    assert result["imported"] == 1
    assert result["total_submitted"] == 2
    assert result["leads"][0]["phone_number"] == "+919876543210"
    assert result["rejected"][0]["errors"] == ["Phone number is too short"]


def test_import_leads_reports_duplicates_and_allows_missing_optional_category(tmp_path):
    store = JsonCallStore(tmp_path)

    first = store.import_leads([
        {"business_name": "Sharma Electronics", "phone_number": "9876543210", "category": "Mobile Accessories"}
    ])
    second = store.import_leads([
        {"business_name": "Duplicate", "phone_number": "98765 43210", "category": "Mobile Accessories"},
        {"business_name": "No Category", "phone_number": "9999999999", "category": ""},
    ])

    assert first["imported"] == 1
    assert second["imported"] == 1
    assert second["duplicates"] == 1
    assert second["rejected"][0]["errors"] == ["Duplicate phone number"]
    assert second["leads"][0]["category"] == ""
    assert len(store.list_leads()) == 2


def test_update_lead_tracks_call_metadata(tmp_path):
    store = JsonCallStore(tmp_path)
    result = store.import_leads([
        {"business_name": "Sharma Electronics", "phone_number": "9876543210", "category": "Mobile Accessories"}
    ])
    lead_id = result["leads"][0]["lead_id"]

    updated = store.update_lead(lead_id, status="calling", last_call_id="call-123", last_call_sid="sid-123")

    assert updated is not None
    assert updated["status"] == "calling"
    assert store.get_lead(lead_id)["last_call_id"] == "call-123"


def test_phone_numbers_are_normalized_for_indian_calling(tmp_path):
    store = JsonCallStore(tmp_path)

    assert store.validate_lead({"business_name": "A", "phone_number": "9251528417", "category": "Retail"})[0]["phone_number"] == "+919251528417"
    assert store.validate_lead({"business_name": "B", "phone_number": "919251528417", "category": "Retail"})[0]["phone_number"] == "+919251528417"
    assert store.validate_lead({"business_name": "C", "phone_number": "+919251528417", "category": "Retail"})[0]["phone_number"] == "+919251528417"


def test_scientific_notation_phone_numbers_expand_before_normalization(tmp_path):
    store = JsonCallStore(tmp_path)

    normalized, errors = store.validate_lead({"business_name": "Sci", "phone_number": "9.19251528417E+11", "category": "Retail"})

    assert errors == []
    assert normalized["phone_number"] == "+919251528417"


def test_excel_lead_template_formats_phone_column_as_text(tmp_path):
    openpyxl = __import__("openpyxl")
    store = JsonCallStore(tmp_path)

    filename, content, media_type = store.export_lead_template()
    workbook = openpyxl.load_workbook(__import__("io").BytesIO(content))
    sheet = workbook.active

    assert filename == "lead-upload-template.xlsx"
    assert media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert sheet["B1"].number_format == "@"
    assert sheet["B2"].number_format == "@"
    assert sheet["B2"].value == "+919876543210"
