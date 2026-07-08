from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from app.core.settings import DATA_DIR
from app.storage.storage import Storage

COLLECTIONS = ("campaigns", "leads", "calls", "responses", "analytics", "settings")


class JsonStorage(Storage):
    """Small JSON-file storage implementation behind the Storage interface."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = data_dir
        self._lock = RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for collection in COLLECTIONS:
            path = self._path(collection)
            if not path.exists():
                path.write_text("[]" if collection != "settings" else "{}", encoding="utf-8")

    def list(self, collection: str) -> list[dict[str, Any]]:
        data = self._read(collection)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    def get(self, collection: str, item_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list(collection) if item.get("id") == item_id), None)

    def create(self, collection: str, item: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            items = self.list(collection)
            record = {**item}
            record.setdefault("id", str(uuid4()))
            items.append(record)
            self._write(collection, items)
            return record

    def update(self, collection: str, item_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            items = self.list(collection)
            for index, item in enumerate(items):
                if item.get("id") == item_id:
                    items[index] = {**item, **updates, "id": item_id}
                    self._write(collection, items)
                    return items[index]
            return None

    def delete(self, collection: str, item_id: str) -> bool:
        with self._lock:
            items = self.list(collection)
            kept = [item for item in items if item.get("id") != item_id]
            if len(kept) == len(items):
                return False
            self._write(collection, kept)
            return True

    def replace_all(self, collection: str, items: list[dict[str, Any]]) -> None:
        self._write(collection, items)

    def settings(self) -> dict[str, Any]:
        data = self._read("settings")
        return data if isinstance(data, dict) else {}

    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        self._write("settings", settings)
        return settings

    def _path(self, collection: str) -> Path:
        if collection not in COLLECTIONS:
            raise ValueError(f"Unknown collection: {collection}")
        return self.data_dir / f"{collection}.json"

    def _read(self, collection: str) -> Any:
        path = self._path(collection)
        with self._lock:
            try:
                text = path.read_text(encoding="utf-8").strip()
                return json.loads(text) if text else ([] if collection != "settings" else {})
            except (OSError, json.JSONDecodeError):
                corrupt = path.with_suffix(path.suffix + ".corrupt")
                try:
                    path.replace(corrupt)
                    path.write_text("[]" if collection != "settings" else "{}", encoding="utf-8")
                except OSError:
                    pass
                return [] if collection != "settings" else {}

    def _write(self, collection: str, data: Any) -> None:
        path = self._path(collection)
        with self._lock:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            tmp.replace(path)
