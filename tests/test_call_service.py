from app.services.call_service import CallResultService
from app.core.models import CallSession


class FakeExtractor:
    def extract(self, session):
        return {"owner_confirmed": "yes"}


class FakeStore:
    def __init__(self):
        self.saved = []

    def append_call(self, session):
        self.saved.append(session)


def test_call_result_service_finalizes_and_persists():
    session = CallSession(call_id="call-service")
    store = FakeStore()

    result = CallResultService(FakeExtractor(), store).finalize(session, "completed")

    assert result.ended_at is not None
    assert result.status == "completed"
    assert result.answers == {"owner_confirmed": "yes"}
    assert store.saved == [session]
