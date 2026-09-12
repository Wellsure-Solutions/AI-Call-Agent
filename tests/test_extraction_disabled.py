from __future__ import annotations

"""Post-call extraction is off, and off means no OpenAI request at all.

Extraction ran one OpenAI call per answered call to derive interest, callback
intent and a structured summary -- fields nobody acted on. Disabling it has to
be complete: a deployment that still queues jobs, or still claims them, or
still constructs a client, is still spending.

Disabled rather than deleted, so these also pin the other half: the transcript
still lands, because `persist_raw` writes it on a path that never touches
OpenAI, and the machinery still works when switched back on.
"""


import asyncio

import pytest

from app.core.models import CallSession
from app.services.call_coordinator import DurableCallCoordinator
from app.services.call_service import CallResultService
from app.storage.sqlite_store import SQLiteCallStore


@pytest.fixture()
def store(tmp_path) -> SQLiteCallStore:
    return SQLiteCallStore(tmp_path / "calls.sqlite3", tmp_path)


def answered_call(store: SQLiteCallStore, phone: str = "+919352596681"):
    lead = store.import_leads([
        {"business_name": "Acme", "phone_number": phone, "category": "Retail", "notes": ""}
    ])["leads"][0]
    call = store.enqueue_call(phone_number=phone, lead_id=lead["lead_id"])
    session = CallSession(
        call_id=call["call_id"], phone_number=phone,
        metadata={"lead_id": lead["lead_id"], "media_connected": True},
    )
    session.add_turn("user", "haan bhai bataiye")
    return call, session


class ExplodingExtractor:
    """Any use at all is a failure, so make it impossible to miss."""

    def extract(self, session):
        raise AssertionError("extraction ran with EXTRACTION_ENABLED off")

    async def extract_async(self, session, timeout):
        raise AssertionError("extraction ran with EXTRACTION_ENABLED off")


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------
def test_extraction_is_off_unless_someone_turns_it_on():
    """The default is what stops the spend. A deployment that has to remember
    to set a variable to avoid being billed is not switched off."""
    from app.core import settings

    assert settings.EXTRACTION_ENABLED is False


def test_an_answered_call_queues_no_extraction_job(store):
    call, session = answered_call(store)

    asyncio.run(CallResultService(ExplodingExtractor(), store).afinalize(session))

    with store.transaction() as db:
        jobs = db.execute("SELECT COUNT(*) FROM extraction_jobs").fetchone()[0]
    assert jobs == 0


def test_the_call_is_not_left_looking_like_work_in_progress(store):
    """`pending` with no job behind it reads as an extraction that never
    finishes, and would sit in the operations view forever."""
    call, session = answered_call(store)

    asyncio.run(CallResultService(ExplodingExtractor(), store).afinalize(session))

    assert store.get_call(call["call_id"])["extraction_status"] == "not_required"


def test_the_transcript_is_still_persisted(store):
    """The thing operators actually read is written by `persist_raw`, which
    never touches OpenAI. Turning extraction off must cost no call data."""
    call, session = answered_call(store)

    asyncio.run(CallResultService(ExplodingExtractor(), store).afinalize(session))

    assert "haan bhai bataiye" in store.get_call(call["call_id"])["transcript"]


def test_the_coordinator_never_claims_an_extraction(store):
    """Belt and braces on the worker side: even against a store that somehow
    had a job, the iteration must not pick it up."""
    claimed: list[str] = []

    class Watchful(SQLiteCallStore):
        def claim_extraction(self, owner, lease_seconds):
            claimed.append(owner)
            return None

    watched = Watchful(store.database_path, store.database_path.parent)
    coordinator = DurableCallCoordinator(watched, 1, 0, extractor=ExplodingExtractor())

    asyncio.run(coordinator.run_once())

    assert claimed == []


def test_no_openai_client_is_ever_constructed(store, monkeypatch):
    """The strongest form of the check: nothing imports its way to a request.

    An AnswerExtractor with no key raises rather than calling out, so this
    watches the constructor instead -- the object that would carry the spend.
    """
    import app.services.answer_extractor as extractor_module

    built: list[object] = []
    original = extractor_module.AnswerExtractor.__init__

    def spy(self, *args, **kwargs):
        built.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(extractor_module.AnswerExtractor, "__init__", spy)

    call, session = answered_call(store)
    asyncio.run(CallResultService(ExplodingExtractor(), store).afinalize(session))

    assert built == []


# ---------------------------------------------------------------------------
# Still works when switched back on
# ---------------------------------------------------------------------------
def test_switching_it_back_on_restores_the_pipeline(store, monkeypatch):
    """Disabled, not deleted. The flag is read at call time precisely so this
    needs no rebuild -- if it ever becomes worth paying for again, it is one
    environment variable."""
    monkeypatch.setattr("app.core.settings.EXTRACTION_ENABLED", True)
    call, session = answered_call(store)

    class Quiet:
        async def extract_async(self, session, timeout):
            return {}

    asyncio.run(CallResultService(Quiet(), store).afinalize(session))

    with store.transaction() as db:
        jobs = db.execute("SELECT COUNT(*) FROM extraction_jobs").fetchone()[0]
    assert jobs == 1
    assert store.get_call(call["call_id"])["extraction_status"] == "pending"
