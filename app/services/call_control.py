from __future__ import annotations

import re

# The agent's only way to end a call itself. Registered on the Deepgram agent
# as a client-side function (no `endpoint`), so the model requesting it
# arrives here as a FunctionCallRequest the adapter answers and acts on.
END_CALL_FUNCTION = "end_call"

# Kept deliberately coarse. The model picks a reason for reporting, and a
# value outside this set is recorded as "other" rather than rejected --
# nothing about hanging up should depend on the model choosing an exact
# string.
END_CALL_REASONS = ("completed", "not_interested", "do_not_call", "wrong_number", "callback_later", "voicemail")

END_CALL_FUNCTION_SCHEMA: dict[str, object] = {
    "name": END_CALL_FUNCTION,
    "description": (
        "End the phone call. Call this immediately after speaking your final "
        "sentence, once the conversation has reached its natural end: the "
        "customer agreed and the follow-up was arranged, the customer declined, "
        "the customer asked not to be called again, or you reached the wrong "
        "person or a voicemail. Never call this in the middle of a conversation "
        "and never call it before you have said goodbye."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "enum": list(END_CALL_REASONS),
                "description": "Why the call is ending.",
            }
        },
        "required": ["reason"],
    },
}


def normalize_end_call_reason(arguments: str | None) -> str:
    """Extract a bounded reason from the model's JSON argument string.

    The arguments arrive as a JSON *string* that the model produced, so it
    can be malformed, truncated, or carry a value outside the enum. None of
    that should stop a hangup, so this never raises -- an unusable value
    simply becomes "other".
    """
    if not arguments:
        return "unspecified"
    import json

    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return "unparseable"
    if not isinstance(parsed, dict):
        return "other"
    reason = str(parsed.get("reason") or "").strip().lower()
    return reason if reason in END_CALL_REASONS else ("other" if reason else "unspecified")


def is_closing_call_message(message, content: str | None = None) -> bool:
    """Detect Deepgram's terminal close signal.

    Recognises the registered end-call function, plus the historical
    `_closing_call` marker that predates real tool registration and could
    still be echoed inside assistant text.
    """
    msg_type = str(getattr(message, "type", ""))
    function_name = str(getattr(message, "name", "") or getattr(message, "function_name", ""))
    text = str(content or getattr(message, "content", "") or "")
    if function_name in {END_CALL_FUNCTION, "_closing_call"}:
        return True
    return "_closing_call" in {msg_type} or "_closing_call()" in text


# Closing lines, as the campaign prompt actually produces them. The prompt
# mandates Devanagari for Hindi with English business terms left in Roman
# script, so a romanized-only list -- which is what this was -- could never
# fire on a real call.
#
# This is a backstop for the model failing to call end_call, not the primary
# path, and it is biased hard toward missing a closure rather than inventing
# one: a false positive hangs up on a customer mid-sentence.
_TERMINAL_ASSISTANT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern) for pattern in (
        # Devanagari farewells
        r"आपका दिन शुभ",
        r"शुभ दिन",
        r"धन्यवाद[,\s]*(?:जी)?[,\s]*(?:नमस्ते|नमस्कार|अलविदा)",
        r"नमस्ते\s*जी[!।.\s]*$",
        r"अलविदा",
        # "our team will call you" -- the scripted close
        r"(?:हमारी|हमारे)\s*(?:team|टीम|executive|एग्जीक्यूटिव).{0,40}?(?:कॉल|call|संपर्क).{0,20}?(?:कर[ें|ेंगे|ेगी|ेगा]|करेंगे)",
        r"team\s+(?:जल्द|will)\s",
        # Romanized variants, retained for transcripts that come back in
        # Roman script despite the prompt.
        r"\baapka din shubh",
        r"\bteam will reach out",
        r"\bteam jald hi",
        r"\bphir baat karte",
        r"\bthank you[,\s]+(?:and\s+)?(?:goodbye|good bye)",
        r"\bthanks[,\s]+(?:and\s+)?(?:goodbye|good bye)",
    )
)


def is_terminal_assistant_text(content: str | None) -> bool:
    """Detect a natural closing line after which phone adapters should hang up."""
    if not content:
        return False
    normalized = " ".join(content.lower().split())
    return any(pattern.search(normalized) for pattern in _TERMINAL_ASSISTANT_PATTERNS)
