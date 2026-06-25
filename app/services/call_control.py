from __future__ import annotations


def is_closing_call_message(message, content: str | None = None) -> bool:
    """Detect Deepgram's terminal _closing_call tool signal."""
    msg_type = str(getattr(message, "type", ""))
    function_name = str(getattr(message, "name", "") or getattr(message, "function_name", ""))
    text = str(content or getattr(message, "content", "") or "")
    return "_closing_call" in {function_name, msg_type} or "_closing_call()" in text
