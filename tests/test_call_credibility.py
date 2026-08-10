from __future__ import annotations

"""The rules that separate this from a fraud call, and the one that is
irreversible.

A business owner in India decides in a few seconds whether an unknown number
is worth listening to. Everything pinned here is a signal that decision runs
on -- and every one of them is also, independently, the honest thing to do.

Separately: do_not_call writes the number to the permanent suppression list.
Nothing else in a call cannot be undone, so the bar for it is pinned here
too. A real transcript showed "Cancel that" drawing the do-not-call closing,
which would have burned that lead forever.
"""

from app.core.prompts import ANSWER_FIELDS, PROMPT


FLAT_PROMPT = " ".join(PROMPT.split())


# ---------------------------------------------------------------------------
# Credibility
# ---------------------------------------------------------------------------
def test_the_call_states_who_and_why_before_asking_anything():
    """Opening with a question about the customer is the fraud-call shape.
    The old opening did exactly that: identity check, then straight into
    qualifying questions, with the reason for the call never stated."""
    assert "Say who you are and why you called BEFORE you ask them anything" in FLAT_PROMPT
    assert "Amazon Business team से" in FLAT_PROMPT
    assert "एक मिनट बात कर सकते हैं?" in FLAT_PROMPT


def test_manufactured_urgency_is_banned_by_name():
    """The single clearest scam marker, and we have no real deadline to
    justify it."""
    for phrase in ("आज ही", "limited period", "ख़त्म हो रहा है"):
        assert phrase in FLAT_PROMPT, phrase
    assert "Never manufacture urgency" in FLAT_PROMPT


def test_the_offer_is_not_dressed_up_as_exclusive():
    assert "Never call it special, exclusive, or selected for them" in FLAT_PROMPT
    assert "anybody can open one" in FLAT_PROMPT


def test_the_customer_is_pointed_at_something_they_can_verify():
    """An unverifiable claim from an unknown number is indistinguishable from
    a fraud, however true it is."""
    assert "Amazon Business is on the Amazon app and website" in FLAT_PROMPT
    assert "Nothing has to be decided, paid, or shared on this call" in FLAT_PROMPT


def test_the_bot_question_has_a_defined_answer():
    """There is a rule for it, and it is one short line rather than something
    the model improvises differently on every call.

    Which way that rule points is a product decision and has been set
    deliberately; this pins only that it is decided somewhere.
    """
    assert "If they ask whether you are a bot, a recording, or an AI" in FLAT_PROMPT
    assert "in one short line, and carry on" in FLAT_PROMPT


def test_a_clear_no_ends_the_pitch():
    assert "Never keep pitching after a clear no. One respectful line, then close." in FLAT_PROMPT
    assert "If they say no a second time, close immediately." in FLAT_PROMPT


def test_credit_is_never_offered_unprompted():
    """An unsolicited loan offer to a stranger is the most scam-flavoured
    thing this script could possibly say, and it was previously delivered to
    everybody as benefit three."""
    assert "Never offer credit to somebody who did not bring it up" in FLAT_PROMPT
    assert "They raise cash flow, payment terms, or udhaar — only then" in FLAT_PROMPT


# ---------------------------------------------------------------------------
# Persuasion, honestly
# ---------------------------------------------------------------------------
def test_the_pitch_waits_until_it_knows_how_they_buy():
    """Guessing instead of asking is what makes a call sound like a
    recording, and it is also what makes the wrong benefit get delivered."""
    assert "FINDING OUT HOW THEY BUY — before any pitch" in FLAT_PROMPT
    assert "personal account से लेते हैं या business के GST number पर?" in FLAT_PROMPT
    assert "Their answers decide everything you say next" in FLAT_PROMPT


def test_gst_is_framed_as_money_already_being_lost():
    """The strongest honest lever available: for a GST-registered business,
    input credit on personal-account purchases is money going out every
    month, not a discount being offered."""
    assert "money they are already losing rather than a deal you are offering" in FLAT_PROMPT
    assert "input credit हर महीने चला जाता है" in FLAT_PROMPT


def test_the_agent_does_not_do_arithmetic_on_the_call():
    """A wrong number spoken aloud by someone claiming to be Amazon is worse
    than no number, and the customer computes it more convincingly anyway."""
    assert "Let them do the arithmetic on their own purchases" in FLAT_PROMPT
    assert "do not ask what they spend" in FLAT_PROMPT


def test_hesitation_is_answered_by_shrinking_the_ask_not_pushing():
    assert "make the ask smaller rather than louder" in FLAT_PROMPT
    assert "nobody is argued into them" in FLAT_PROMPT


# ---------------------------------------------------------------------------
# The irreversible one
# ---------------------------------------------------------------------------
def test_do_not_call_is_marked_as_permanent_and_unique():
    assert "blocks this number permanently" in FLAT_PROMPT
    assert "It cannot be undone" in FLAT_PROMPT


def test_the_phrases_that_do_not_mean_do_not_call_are_listed():
    """"Cancel that" drew the do-not-call closing on a real call. It meant
    cancel the request, not never contact me."""
    for phrase in ("cancel that", "अभी नहीं", "busy हूँ", "interest नहीं है"):
        assert phrase in FLAT_PROMPT, phrase
    assert "It does NOT apply to" in FLAT_PROMPT


def test_the_phrases_that_do_mean_it_are_listed_too():
    for phrase in ("दोबारा call मत करना", "remove my number"):
        assert phrase in FLAT_PROMPT, phrase


def test_uncertainty_resolves_away_from_the_permanent_outcome():
    assert "If you are not certain, it is not_interested" in FLAT_PROMPT


def test_extraction_asks_the_same_narrow_question():
    """The prompt deciding correctly does not help if the extractor then
    scores the same transcript as a DND request -- suppression is written
    from the extracted answer, not from the call."""
    question = next(f for f in ANSWER_FIELDS if f.name == "do_not_call_requested").question
    assert "ask not to be called again" in question or "opt out" in question


# ---------------------------------------------------------------------------
# What must not have been lost in the rewrite
# ---------------------------------------------------------------------------
def test_the_locked_facts_are_still_exactly_three_and_still_hedged():
    for fact in ("अठारह प्रतिशत", "दस प्रतिशत cashback", "साठ हज़ार रुपये"):
        assert fact in FLAT_PROMPT, fact
    assert "Never quote a number that is not here" in FLAT_PROMPT
    assert 'Never say "guaranteed"' in FLAT_PROMPT


def test_the_credential_rules_survived():
    assert "Never ask for OTP, CVV, card number, UPI PIN, password" in FLAT_PROMPT
    assert "Never claim to process a payment, refund, or account change yourself" in FLAT_PROMPT


def test_the_switch_is_still_described_as_an_upgrade_not_a_signup():
    assert "Not a new signup, no fee, nothing to install" in FLAT_PROMPT
