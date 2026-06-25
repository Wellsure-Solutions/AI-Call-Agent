from __future__ import annotations

import re

_STORE_ASSIGNMENT_RE = re.compile(
    r"\b(?:store\s*)?\[?['\"]?[a-zA-Z_][a-zA-Z0-9_]*['\"]?\]?\s*=\s*['\"]?[^'\".\n]+['\"]?\s*",
    flags=re.IGNORECASE,
)
_INTERNAL_PREFIX_RE = re.compile(r"\b(?:store|set)\s*:\s*", flags=re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def strip_spoken_internal_commands(text: str | None) -> str:
    """Remove leaked internal assignment text from assistant transcript messages."""
    if not text:
        return ""
    cleaned = _STORE_ASSIGNMENT_RE.sub("", text)
    cleaned = _INTERNAL_PREFIX_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned
