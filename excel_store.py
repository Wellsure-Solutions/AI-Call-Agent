from __future__ import annotations

import html
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile
from xml.etree import ElementTree as ET

from models import CallSession
from prompts import ANSWER_FIELDS

SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
HEADERS = [
    "call_id",
    "started_at",
    "ended_at",
    "duration_seconds",
    "status",
    *(field.name for field in ANSWER_FIELDS),
    "transcript",
]


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _cell(reference: str, value: object) -> str:
    safe = html.escape("" if value is None else str(value))
    return f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{safe}</t></is></c>'


def _row(index: int, values: list[object]) -> str:
    cells = "".join(_cell(f"{_column_name(col)}{index}", value) for col, value in enumerate(values, start=1))
    return f'<row r="{index}">{cells}</row>'


def _build_workbook(rows: list[list[object]]) -> dict[str, str]:
    dimension = f"A1:{_column_name(len(HEADERS))}{max(1, len(rows))}"
    sheet_rows = "".join(_row(index, row) for index, row in enumerate(rows, start=1))
    sheet = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{SHEET_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="{dimension}"/>
  <sheetViews><sheetView workbookViewId="0"/></sheetViews>
  <sheetFormatPr defaultRowHeight="15"/>
  <sheetData>{sheet_rows}</sheetData>
</worksheet>'''
    return {
        "[Content_Types].xml": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>''',
        "_rels/.rels": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>''',
        "xl/workbook.xml": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Call Answers" sheetId="1" r:id="rId1"/></sheets>
</workbook>''',
        "xl/_rels/workbook.xml.rels": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>''',
        "xl/worksheets/sheet1.xml": sheet,
    }


def _read_rows(path: Path) -> list[list[str]]:
    if not path.exists():
        return [HEADERS]
    with zipfile.ZipFile(path) as workbook:
        sheet_xml = workbook.read("xl/worksheets/sheet1.xml")
    root = ET.fromstring(sheet_xml)
    rows: list[list[str]] = []
    for row in root.findall(f".//{{{SHEET_NS}}}row"):
        values = []
        for cell in row.findall(f"{{{SHEET_NS}}}c"):
            text = cell.find(f".//{{{SHEET_NS}}}t")
            values.append(text.text if text is not None and text.text is not None else "")
        rows.append(values)
    return rows or [HEADERS]


class ExcelAnswerStore:
    """Append-only .xlsx answer store with no runtime database dependency."""

    def __init__(self, workbook_path: Path) -> None:
        self.workbook_path = workbook_path

    def append_call(self, session: CallSession) -> None:
        self.workbook_path.parent.mkdir(parents=True, exist_ok=True)
        rows = _read_rows(self.workbook_path)
        row = [
            session.call_id,
            session.started_at.isoformat(),
            session.ended_at.isoformat() if session.ended_at else "",
            session.duration_seconds,
            session.status,
            *(session.answers.get(field.name, "unknown") for field in ANSWER_FIELDS),
            session.transcript_text,
        ]
        rows.append(row)
        payload = _build_workbook(rows)
        with NamedTemporaryFile("wb", delete=False, suffix=".xlsx", dir=self.workbook_path.parent) as tmp:
            temp_path = Path(tmp.name)
        try:
            with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
                for name, content in payload.items():
                    workbook.writestr(name, content)
            temp_path.replace(self.workbook_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
