from __future__ import annotations

import csv
import io
import json
from typing import Any
from openpyxl import Workbook

HEADERS = ["Business Name", "Category", "Phone", "Campaign", "Status", "Outcome", "Interested", "Decision Maker", "Callback Requested", "Duration", "Summary", "Transcript", "JSON Fields", "Timestamp"]


def rows(calls: list[dict[str, Any]], responses: list[dict[str, Any]], campaigns: list[dict[str, Any]]) -> list[list[Any]]:
    rb = {r.get("call_id"): r for r in responses}
    cn = {c.get("id"): c.get("name") for c in campaigns}
    out = []
    for c in calls:
        r = rb.get(c.get("id"), {})
        out.append([c.get("business_name"), c.get("category"), c.get("phone"), cn.get(c.get("campaign_id"), c.get("campaign")), c.get("status"), c.get("outcome"), r.get("interested"), r.get("decision_maker"), r.get("callback_requested"), c.get("duration") or c.get("duration_seconds"), r.get("summary") or c.get("summary"), c.get("transcript"), json.dumps(r, ensure_ascii=False), c.get("started_at")])
    return out


def csv_bytes(data_rows: list[list[Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(HEADERS); writer.writerows(data_rows)
    return buf.getvalue().encode("utf-8")


def xlsx_bytes(data_rows: list[list[Any]]) -> bytes:
    wb = Workbook(); ws = wb.active; ws.title = "Calls Export"; ws.append(HEADERS)
    for row in data_rows: ws.append(row)
    for cell in ws[1]: cell.style = "Headline 3"
    bio = io.BytesIO(); wb.save(bio); wb.close(); return bio.getvalue()
