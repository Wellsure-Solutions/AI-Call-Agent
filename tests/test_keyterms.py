from __future__ import annotations

"""Biasing the recogniser toward the words this campaign actually uses.

The transcripts show the failure this addresses. "GST से लेते हैं" came back
as "जी, ऐसे से लेते हैं?" -- and a misheard GST answer is not cosmetic,
because whether the customer has GST is what chooses which benefit the agent
gives next.

Deepgram accepts these on the listen provider and they cost nothing per call.
"""

from app.core.settings import DEEPGRAM_KEYTERMS
from app.integrations.deepgram.config import MAX_KEYTERMS, _keyterms_for, get_agent_settings


def listen(context=None) -> dict:
    return get_agent_settings(context, "twilio").dict()["agent"]["listen"]["provider"]


def test_the_campaign_vocabulary_reaches_the_recogniser():
    terms = listen()["keyterms"]
    for word in ("Wellsure", "Amazon", "Seller Central", "returns", "commission"):
        assert word in terms, word


def test_this_lead_s_business_name_is_boosted_for_its_own_call():
    """The agent opens by asking whether it reached that business, and the
    answer usually repeats the name back."""
    terms = listen({"business_name": "Sharma Electronics"})["keyterms"]
    assert "Sharma Electronics" in terms
    assert "Sharma Electronics" not in listen()["keyterms"]


def test_a_lead_without_a_name_changes_nothing():
    assert listen({"business_name": ""})["keyterms"] == listen()["keyterms"]
    assert listen({"business_name": None})["keyterms"] == listen()["keyterms"]


def test_lead_data_cannot_flood_the_list():
    """Lead data is untrusted input. A long list dilutes the bias, and past
    some point just makes unlikely words easier to hallucinate out of noise."""
    terms = _keyterms_for({"business_name": "x" * 5000})
    assert len(terms) <= MAX_KEYTERMS
    assert max(len(term) for term in terms) <= 60


def test_whitespace_and_newlines_in_lead_data_are_flattened():
    terms = _keyterms_for({"business_name": "  Sharma\n\tElectronics  "})
    assert "Sharma Electronics" in terms


def test_a_duplicate_business_name_is_not_sent_twice():
    """Deduplicated case-insensitively, keeping the configured spelling."""
    terms = _keyterms_for({"business_name": "wellsure"})
    assert sum(1 for t in terms if t.casefold() == "wellsure") == 1
    assert "Wellsure" in terms, "the configured spelling wins over the lead's"


def test_the_list_stays_short_enough_to_be_worth_something():
    assert 0 < len(DEEPGRAM_KEYTERMS) <= MAX_KEYTERMS


def test_turn_taking_settings_are_untouched_by_this():
    provider = listen({"business_name": "Sharma Electronics"})
    assert provider["model"] == "flux-general-multi"
    assert provider["language_hints"] == ["hi", "en"]
    assert "eot_threshold" in provider


def test_settings_still_serialize_with_keyterms_attached():
    import json
    payload = json.dumps(get_agent_settings({"business_name": "Sharma Electronics"}, "twilio").dict(), default=str)
    assert "keyterms" in payload
    assert "UNTRUSTED LEAD DATA" in payload
