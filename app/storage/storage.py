from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Storage(ABC):
    """Repository-style storage boundary for dashboard data.

    Business logic must use this interface instead of reading JSON files
    directly so a PostgreSQL implementation can replace JsonStorage later.
    """

    @abstractmethod
    def list(self, collection: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get(self, collection: str, item_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def create(self, collection: str, item: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def update(self, collection: str, item_id: str, updates: dict[str, Any]) -> dict[str, Any] | None: ...

    @abstractmethod
    def delete(self, collection: str, item_id: str) -> bool: ...

    @abstractmethod
    def replace_all(self, collection: str, items: list[dict[str, Any]]) -> None: ...
