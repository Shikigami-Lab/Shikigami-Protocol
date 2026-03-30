"""TodoStore — per-profile todo list, stored in profiles/{id}/todos.json"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TodoItem:
    id: str
    content: str
    done: bool = False
    created_at: float = 0.0
    due_date: Optional[float] = None
    priority: int = 3   # 1 (low) – 5 (high), default 3

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TodoItem":
        return cls(
            id=d["id"],
            content=d.get("content", ""),
            done=d.get("done", False),
            created_at=d.get("created_at", 0.0),
            due_date=d.get("due_date"),
            priority=d.get("priority", 3),
        )


class TodoStore:
    """CRUD for todos.json inside a profile's storage_root."""

    def __init__(self, storage_root: str):
        self._path = os.path.join(storage_root, "todos.json")
        self._items: List[TodoItem] = self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> List[TodoItem]:
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [TodoItem.from_dict(d) for d in data]
        except Exception as e:
            logger.warning("[TodoStore] load failed: %s", e)
            return []

    def _save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump([t.to_dict() for t in self._items], f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[TodoStore] save failed: %s", e)

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def add(self, content: str, due_date: Optional[float] = None, priority: int = 3) -> TodoItem:
        item = TodoItem(
            id=uuid.uuid4().hex[:8],
            content=content.strip(),
            done=False,
            created_at=time.time(),
            due_date=due_date,
            priority=max(1, min(5, priority)),
        )
        self._items.append(item)
        self._save()
        logger.info("[TodoStore] add id=%s content=%s", item.id, item.content)
        return item

    def _fuzzy_find(self, query: str) -> Optional[TodoItem]:
        """Substring match against pending items first, then all."""
        q = query.strip().lower()
        for item in self._items:
            if not item.done and q in item.content.lower():
                return item
        for item in self._items:
            if q in item.content.lower():
                return item
        return None

    def complete(self, query: str) -> Optional[TodoItem]:
        item = self._fuzzy_find(query)
        if item:
            item.done = True
            self._save()
            logger.info("[TodoStore] complete id=%s", item.id)
        return item

    def delete(self, query: str) -> Optional[TodoItem]:
        item = self._fuzzy_find(query)
        if item:
            self._items.remove(item)
            self._save()
            logger.info("[TodoStore] delete id=%s", item.id)
        return item

    def get_by_id(self, todo_id: str) -> Optional[TodoItem]:
        for item in self._items:
            if item.id == todo_id:
                return item
        return None

    def update(self, todo_id: str, done: Optional[bool] = None, content: Optional[str] = None) -> Optional[TodoItem]:
        item = self.get_by_id(todo_id)
        if not item:
            return None
        if done is not None:
            item.done = done
        if content is not None:
            item.content = content.strip()
        self._save()
        return item

    def get_pending(self) -> List[TodoItem]:
        """Return undone items sorted by priority (high first), then created_at."""
        pending = [t for t in self._items if not t.done]
        return sorted(pending, key=lambda t: (-t.priority, t.created_at))

    def delete_by_id(self, todo_id: str) -> Optional[TodoItem]:
        item = self.get_by_id(todo_id)
        if item:
            self._items.remove(item)
            self._save()
            logger.info("[TodoStore] delete_by_id id=%s", todo_id)
        return item

    def clear_done(self) -> int:
        before = len(self._items)
        self._items = [t for t in self._items if not t.done]
        removed = before - len(self._items)
        if removed:
            self._save()
        return removed

    def get_all(self) -> List[TodoItem]:
        return list(self._items)
