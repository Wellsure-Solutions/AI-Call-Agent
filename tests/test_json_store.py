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


def test_import_leads_reports_duplicates_and_missing_required_fields(tmp_path):
    store = JsonCallStore(tmp_path)

    first = store.import_leads([
        {"business_name": "Sharma Electronics", "phone_number": "9876543210", "category": "Mobile Accessories"}
    ])
    second = store.import_leads([
        {"business_name": "Duplicate", "phone_number": "98765 43210", "category": "Mobile Accessories"},
        {"business_name": "No Category", "phone_number": "9999999999", "category": ""},
    ])

    assert first["imported"] == 1
    assert second["imported"] == 0
    assert second["duplicates"] == 1
    assert second["rejected"][0]["errors"] == ["Duplicate phone number"]
    assert second["rejected"][1]["errors"] == ["Missing category"]
    assert len(store.list_leads()) == 1
