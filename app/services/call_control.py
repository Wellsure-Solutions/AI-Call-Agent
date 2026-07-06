from __future__ import annotations


def is_closing_call_message(message, content: str | None = None) -> bool:
    """Detect Deepgram's terminal _closing_call tool signal."""
    msg_type = str(getattr(message, "type", ""))
    function_name = str(getattr(message, "name", "") or getattr(message, "function_name", ""))
    text = str(content or getattr(message, "content", "") or "")
    return "_closing_call" in {function_name, msg_type} or "_closing_call()" in text


_TERMINAL_ASSISTANT_PHRASES = (
    "aapka din shubh",
    "team will reach out",
    "team jald hi",
    "phir baat karte",
    "thank you, goodbye",
    "thanks, goodbye",
)


def is_terminal_assistant_text(content: str | None) -> bool:
    """Detect natural closing lines after which phone adapters should hang up."""
    if not content:
        return False
    normalized = " ".join(content.lower().split())
    return any(phrase in normalized for phrase in _TERMINAL_ASSISTANT_PHRASES)
