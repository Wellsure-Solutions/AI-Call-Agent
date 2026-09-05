"""Cheap, safe file-descriptor accounting for diagnosing leaks like this one.

Nothing here should ever be on a hot path: `open_fd_count()` lists
`/proc/self/fd`, which is O(open descriptors) -- fine once per call or on a
slow periodic timer, wrong per-frame or per-request. `None` on anything but
Linux (no `/proc`) or if listing it fails for any reason, so a call site can
drop it into a log's `extra=` unconditionally without a platform check or a
try/except of its own.
"""

from __future__ import annotations

import os


def open_fd_count() -> int | None:
    """The process's current open file descriptor count, or None if unknown."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None
