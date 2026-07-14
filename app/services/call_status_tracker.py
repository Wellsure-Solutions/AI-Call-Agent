from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CallStatusTracker:
    """Small in-memory status feed for dashboard call progress.

    This is intentionally isolated from persistence so it can be replaced by a
    database/queue later without touching Twilio call control.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._statuses: dict[str, dict[str, Any]] = {}

    def upsert(self, call_id: str, status: str, **metadata: Any) -> dict[str, Any]:
        with self._lock:
            current = self._statuses.get(call_id, {"call_id": call_id, "created_at": _now(), "events": []})
            current.update({key: value for key, value in metadata.items() if value is not None})
            current["status"] = status
            current["updated_at"] = _now()
            current.setdefault("events", []).append({"status": status, "timestamp": current["updated_at"]})
            self._statuses[call_id] = current
            return dict(current)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((dict(value) for value in self._statuses.values()), key=lambda item: item.get("updated_at", ""), reverse=True)

    def get(self, call_id: str) -> dict[str, Any] | None:
        with self._lock:
            current = self._statuses.get(call_id)
            return dict(current) if current else None


call_status_tracker = CallStatusTracker()
