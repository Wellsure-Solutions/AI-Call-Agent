from __future__ import annotations

"""What counts as a yes, and asking when to call back.

Both taken from real transcripts. In one, the agent asked "क्या आप इसके लिए
open हैं?", the customer said "ठीक है." -- and the agent treated that as
agreement. In the other the customer said "Hello." (they could not hear) and
the agent closed the call as a win. Neither call ever asked when to call back,
and both signed off promising a call "कल" that nobody had agreed to.

These are prompt rules, so these tests pin the rules rather than the model's
behaviour. What the model does with them only a live batch can say.
"""

from app.core.prompts import ANSWER_FIELDS, PROMPT


def flat(text: str) -> str:
    """The prompt wraps across lines; rules have to be matched on one."""
    return " ".join(text.split())


FLAT_PROMPT = flat(PROMPT)


def field(name: str):
    return next(f for f in ANSWER_FIELDS if f.name == name)


# ---------------------------------------------------------------------------
# Politeness is not consent
# ---------------------------------------------------------------------------
def test_the_prompt_names_the_acknowledgements_that_are_not_agreement():
    """Every one of these appeared in a real call and was read as a yes."""
    for phrase in ("ठीक है", "अच्छा", "ok", "हाँ हाँ", "जी"):
        assert phrase in FLAT_PROMPT, phrase
    assert "NOT agreement" in FLAT_PROMPT


def test_a_reply_that_does_not_answer_the_question_is_not_agreement():
    """The "Hello." case: the customer could not hear, and the agent closed
    the call as a win."""
    assert "hello?" in FLAT_PROMPT
    assert "does not answer what you asked" in FLAT_PROMPT


def test_the_prompt_says_what_agreement_actually_sounds_like():
    """Listing only what to reject leaves the model with no positive test."""
    assert "only makes sense as a yes" in FLAT_PROMPT
    for phrase in ("हाँ करवा दीजिए", "कर दीजिए"):
        assert phrase in FLAT_PROMPT


def test_an_unclear_answer_is_re_asked_exactly_once():
    """Once, because a customer asked three times is being pressured, and
    zero times is the bug being fixed."""
    assert "ask once more" in FLAT_PROMPT
    assert "do not ask a third time" in FLAT_PROMPT
    assert "do not claim they agreed" in FLAT_PROMPT


# ---------------------------------------------------------------------------
# The callback time
# ---------------------------------------------------------------------------
def test_asking_for_a_callback_time_is_a_step_of_its_own():
    """It was previously conditional -- "only if it has not come up" -- and
    the model skipped it on every call."""
    assert "Ask when it suits them for the team to call. Never skip this." in FLAT_PROMPT
    assert "आपको किस time call करना ठीक रहेगा?" in FLAT_PROMPT


def test_the_agent_may_not_invent_a_callback_day():
    """Both real calls promised a call "कल" that nobody had agreed to."""
    assert "never say \"कल\" until they have given one" in FLAT_PROMPT
    assert "never invent a day" in FLAT_PROMPT


def test_a_customer_who_will_not_pick_a_time_is_accepted():
    """Otherwise the rule turns into pressing someone who already said yes."""
    assert "कभी भी" in FLAT_PROMPT
    assert "accept it and move on" in FLAT_PROMPT


def test_the_step_gate_puts_confirmation_and_the_time_before_the_close():
    gate = FLAT_PROMPT.split("STEP GATE")[1].split("###")[0]
    assert gate.index("Get a real yes") < gate.index("Ask when it suits them")
    assert gate.index("Ask when it suits them") < gate.index("Close.")


# ---------------------------------------------------------------------------
# What gets recorded
# ---------------------------------------------------------------------------
def test_the_callback_time_is_extracted_verbatim():
    """A time nobody wrote down is a time the onboarding team cannot use."""
    assert field("callback_time").allowed_values == ("free_text",)
    assert "own words" in field("callback_time").question


def test_extraction_holds_the_same_bar_for_agreement_as_the_call_does():
    """If the extractor scores "ठीक है" as a yes, the call was still misread --
    just later, and in the report the sales team acts on."""
    question = field("callback_approved").question
    assert "explicitly" in question
    assert "ठीक है" in question
    assert "unknown" in question


# ---------------------------------------------------------------------------
# The closing still cannot be skipped
# ---------------------------------------------------------------------------
def test_the_sign_off_is_ordered_before_the_tool_call():
    """The other reported failure: end_call arrived with no assistant speech
    at all since the customer's turn, and the line simply went dead."""
    assert "sign-off is spoken FIRST and the tool comes after" in FLAT_PROMPT
    assert "you owe them a closing before the line drops" in FLAT_PROMPT


def test_every_outcome_still_has_a_spoken_close():
    assert "Every call ends with a spoken close" in FLAT_PROMPT
    for outcome in ("Not interested", "Do not call again", "Busy / later", "Wrong number"):
        assert outcome in FLAT_PROMPT
