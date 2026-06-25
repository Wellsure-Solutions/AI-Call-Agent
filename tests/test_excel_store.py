import pytest

openpyxl = pytest.importorskip("openpyxl", reason="openpyxl is unavailable in this environment")
load_workbook = openpyxl.load_workbook

from app.services.call_service import CallResultService
from app.storage.excel_store import ExcelAnswerStore, HEADERS, SHEET_NAME
from app.core.models import CallSession


class FakeExtractor:
    def extract(self, session):
        return {
            "owner_confirmed": "yes",
            "interested": "no",
            "already_selling_online": "unknown",
            "gst_available": "unknown",
            "callback_approved": "unknown",
            "callback_time": "unknown",
        }


def test_call_result_service_saves_xlsx_with_openpyxl(tmp_path):
    workbook_path = tmp_path / "answers.xlsx"
    session = CallSession(call_id="call-2")
    session.add_turn("agent", "Kya main business owner ya decision maker se baat kar raha hoon?")
    session.add_turn("user", "Haan main malik hoon")
    session.add_turn("agent", "Kya aap online marketplaces par apne products bechne mein interested hain?")
    session.add_turn("user", "Nahi interested nahi")

    CallResultService(FakeExtractor(), ExcelAnswerStore(workbook_path)).finalize(session, "completed")

    workbook = load_workbook(workbook_path)
    sheet = workbook[SHEET_NAME]
    assert [cell.value for cell in sheet[1]] == HEADERS
    assert sheet.max_row == 2
    values = [cell.value for cell in sheet[2]]
    assert values[0] == "call-2"
    assert values[4] == "completed"
    assert values[5] == "yes"
    assert values[6] == "no"
    assert "malik" in values[-1]
    workbook.close()
