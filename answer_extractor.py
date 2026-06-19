import re
from collections.abc import Iterable

from models import CallSession
from prompts import ANSWER_FIELDS

YES_WORDS = ("yes", "haan", "han", "bilkul", "interested", "bataiye", "details", "try", "kar sakte", "theek")
NO_WORDS = ("no", "nahi", "nahin", "bilkul nahi", "zarurat nahi", "interested nahi", "mat", "not interested")
OWNER_WORDS = ("owner", "malik", "decision", "main hi", "mai hi", "haan ji", "bol raha")
ONLINE_WORDS = ("amazon", "flipkart", "meesho", "online", "marketplace", "website")
GST_WORDS = ("gst", "registration", "number")
CALLBACK_WORDS = ("callback", "call back", "phone", "sampark", "baad", "kal", "shaam", "subah")


def _last_customer_answer_after(transcript: Iterable[tuple[str, str]], markers: tuple[str, ...]) -> str:
    seen_marker = False
    latest = ""
    for role, content in transcript:
        lowered = content.lower()
        if role != "user" and any(marker in lowered for marker in markers):
            seen_marker = True
            continue
        if seen_marker and role == "user":
            latest = lowered
            seen_marker = False
    return latest


def _classify_yes_no(text: str) -> str:
    if not text:
        return "unknown"
    if any(word in text for word in NO_WORDS):
        return "no"
    if any(word in text for word in YES_WORDS):
        return "yes"
    return "unknown"


def _extract_callback_time(text: str) -> str:
    if not text:
        return "unknown"
    patterns = [
        r"(?:kal|tomorrow)(?:\s+(?:subah|shaam|morning|evening))?",
        r"(?:aaj|today)(?:\s+(?:subah|shaam|morning|evening))?",
        r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm|baje)?\b",
        r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
    ]
    matches = []
    for pattern in patterns:
        matches.extend(re.findall(pattern, text, flags=re.IGNORECASE))
    return ", ".join(dict.fromkeys(match.strip() for match in matches if match.strip())) or "unknown"


class AnswerExtractor:
    """Best-effort temporary extractor driven by prompt answer fields, without a DB."""

    def extract(self, session: CallSession) -> dict[str, str]:
        transcript = [(turn.role, turn.content) for turn in session.transcript]
        customer_text = " ".join(content.lower() for role, content in transcript if role == "user")

        answers = {field.name: "unknown" for field in ANSWER_FIELDS}
        answers["owner_confirmed"] = self._owner_confirmed(transcript, customer_text)
        answers["interested"] = _classify_yes_no(
            _last_customer_answer_after(transcript, ("interested", "products bechne", "marketplaces"))
        )
        answers["already_selling_online"] = _classify_yes_no(
            _last_customer_answer_after(transcript, ("online platform", "selling kar", "marketplace"))
        )
        answers["gst_available"] = _classify_yes_no(_last_customer_answer_after(transcript, GST_WORDS))
        callback_answer = _last_customer_answer_after(transcript, CALLBACK_WORDS)
        answers["callback_approved"] = _classify_yes_no(callback_answer)
        answers["callback_time"] = _extract_callback_time(customer_text)
        if answers["callback_approved"] == "unknown" and answers["callback_time"] != "unknown":
            answers["callback_approved"] = "yes"
        return answers

    def _owner_confirmed(self, transcript: list[tuple[str, str]], customer_text: str) -> str:
        answer = _last_customer_answer_after(transcript, ("owner", "decision maker", "malik"))
        classified = _classify_yes_no(answer)
        if classified != "unknown":
            return classified
        if any(word in customer_text for word in OWNER_WORDS):
            return "yes"
        return "unknown"
