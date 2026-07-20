from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CallStatusTracker:
    """Strictly bounded legacy compatibility cache; never authoritative."""

    def __init__(self, max_calls: int = 256, max_events_per_call: int = 20) -> None:
        self.max_calls = max_calls
        self.max_events_per_call = max_events_per_call
        self._statuses: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def upsert(self, call_id: str, status: str, **metadata: Any) -> dict[str, Any]:
        current = self._statuses.pop(call_id, {"call_id": call_id, "created_at": _now(), "events": []})
        current.update({key: value for key, value in metadata.items() if value is not None})
        current["status"] = status
        current["updated_at"] = _now()
        events = current.setdefault("events", [])
        events.append({"status": status, "timestamp": current["updated_at"]})
        del events[:-self.max_events_per_call]
        self._statuses[call_id] = current
        while len(self._statuses) > self.max_calls:
            self._statuses.popitem(last=False)
        return dict(current)

    def list(self) -> list[dict[str, Any]]:
        return sorted((dict(value) for value in self._statuses.values()), key=lambda item: item["updated_at"], reverse=True)

    def get(self, call_id: str) -> dict[str, Any] | None:
        current = self._statuses.get(call_id)
        return dict(current) if current else None


call_status_tracker = CallStatusTracker()
