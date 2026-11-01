"""TodoStore — per-profile todo list, stored in profiles/{id}/todos.json"""
from __future__ import annotations

import json
import logging
import os
from pathlib import PurePosixPath, PureWindowsPath
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# UI requests and tool calls can hold different stores for the same profile.
# These locks serialize read/modify/replace within the host process.
_FILE_LOCKS = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _validate_owner(owner: str) -> str:
    if owner not in ("user", "ai"):
        raise ValueError("owner must be user or ai")
    return owner


def _content(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError("content must be nonblank and at most 2000 characters")
    return value.strip()


def _relative_path(value: str) -> str:
    """Normalize references lexically; workspace existence/ownership is checked by callers."""
    if not isinstance(value, str):
        raise ValueError("path must be a relative string")
    if not value:
        return ""
    path = value.replace("\\", "/")
    if (PureWindowsPath(path).drive or path.startswith("/")
            or ".." in path.split("/") or ":" in path
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in path)):
        raise ValueError("path must be relative without traversal")
    return PurePosixPath(path).as_posix()


@dataclass
class TodoItem:
    id: str
    content: str
    done: bool = False
    created_at: float = 0.0
    due_date: Optional[float] = None
    priority: int = 3   # 1 (low) – 5 (high), default 3
    owner: str = "user"
    source_path: str = ""
    evidence_path: str = ""
    last_run_id: str = ""

    def __post_init__(self):
        _validate_owner(self.owner)
        self.source_path = _relative_path(self.source_path)
        self.evidence_path = _relative_path(self.evidence_path)
        if not isinstance(self.last_run_id, str):
            raise ValueError("last_run_id must be a string")

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
            owner=d.get("owner", "user"),
            source_path=d.get("source_path", ""),
            evidence_path=d.get("evidence_path", ""),
            last_run_id=d.get("last_run_id", ""),
        )


class TodoStore:
    """CRUD for todos.json inside a profile's storage_root."""

    def __init__(self, storage_root: str):
        self._path = os.path.normcase(os.path.realpath(os.path.join(storage_root, "todos.json")))
        with _FILE_LOCKS_GUARD:
            self._lock = _FILE_LOCKS.setdefault(self._path, threading.RLock())
        with self._lock:
            self._items: List[TodoItem] = self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> List[TodoItem]:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError("todos.json must contain a list of objects")
        items = [TodoItem.from_dict(d) for d in data]
        ids = [item.id for item in items]
        if (any(not isinstance(value, str) or not value.strip() for value in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("todos.json must contain unique nonblank string ids")
        return items

    def _save(self):
        directory = os.path.dirname(self._path)
        os.makedirs(directory, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".todos-", suffix=".tmp", delete=False) as f:
                temporary = f.name
                json.dump([t.to_dict() for t in self._items], f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self._path)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def add(self, content: str, due_date: Optional[float] = None, priority: int = 3, *,
            owner: str = "user", source_path: str = "", item_id: Optional[str] = None,
            last_run_id: str = "") -> TodoItem:
        content = _content(content)
        _validate_owner(owner)
        source_path = _relative_path(source_path)
        if item_id is not None and (not isinstance(item_id, str) or not item_id.strip()):
            raise ValueError("item_id must be a nonblank string")
        with self._lock:
            self._items = self._load()
            if item_id is not None:
                existing = self._find_by_id(item_id)
                if existing is not None:
                    if existing.owner != owner:
                        raise ValueError("item_id already belongs to a different owner")
                    return existing  # Retries never reset completion or evidence.
            else:
                item_id = uuid.uuid4().hex[:8]
                while self._find_by_id(item_id) is not None:
                    item_id = uuid.uuid4().hex[:8]
            item = TodoItem(
                id=item_id, content=content, created_at=time.time(), due_date=due_date,
                priority=max(1, min(5, priority)), owner=owner, source_path=source_path,
                last_run_id=last_run_id,
            )
            self._items.append(item)
            self._save()
            logger.info("[TodoStore] add id=%s content=%s", item.id, item.content)
            return item

    def _fuzzy_find(self, query: str) -> Optional[TodoItem]:
        """Substring match user todos only, pending first. Caller holds the lock."""
        q = query.strip().lower()
        for item in self._items:
            if item.owner == "user" and not item.done and q in item.content.lower():
                return item
        for item in self._items:
            if item.owner == "user" and q in item.content.lower():
                return item
        return None

    def complete(self, query: str) -> Optional[TodoItem]:
        with self._lock:
            self._items = self._load()
            item = self._fuzzy_find(query)
            if item:
                item.done = True
                self._save()
                logger.info("[TodoStore] complete id=%s", item.id)
            return item

    def delete(self, query: str) -> Optional[TodoItem]:
        with self._lock:
            self._items = self._load()
            item = self._fuzzy_find(query)
            if item:
                self._items.remove(item)
                self._save()
                logger.info("[TodoStore] delete id=%s", item.id)
            return item

    def get_by_id(self, todo_id: str) -> Optional[TodoItem]:
        with self._lock:
            self._items = self._load()
            return self._find_by_id(todo_id)

    def _find_by_id(self, todo_id: str) -> Optional[TodoItem]:
        for item in self._items:
            if item.id == todo_id:
                return item
        return None

    def update(self, todo_id: str, done: Optional[bool] = None, content: Optional[str] = None, *,
               evidence_path: Optional[str] = None, last_run_id: Optional[str] = None,
               expected_owner: Optional[str] = None) -> Optional[TodoItem]:
        if expected_owner is not None:
            _validate_owner(expected_owner)
        if content is not None:
            content = _content(content)
        if evidence_path is not None:
            evidence_path = _relative_path(evidence_path)
        if last_run_id is not None and not isinstance(last_run_id, str):
            raise ValueError("last_run_id must be a string")
        if done is not None and not isinstance(done, bool):
            raise ValueError("done must be a boolean")
        with self._lock:
            self._items = self._load()
            item = self._find_by_id(todo_id)
            if item is None or (expected_owner is not None and item.owner != expected_owner):
                return None
            if done is not None:
                item.done = done
            if content is not None:
                item.content = content
            if evidence_path is not None:
                item.evidence_path = evidence_path
            if last_run_id is not None:
                item.last_run_id = last_run_id
            self._save()
            return item

    def get_pending(self, owner: Optional[str] = None) -> List[TodoItem]:
        """Return undone items sorted by priority (high first), then created_at."""
        if owner is not None:
            _validate_owner(owner)
        with self._lock:
            self._items = self._load()
            pending = [t for t in self._items if not t.done and (owner is None or t.owner == owner)]
            return sorted(pending, key=lambda t: (-t.priority, t.created_at))

    def delete_by_id(self, todo_id: str, *, expected_owner: Optional[str] = None) -> Optional[TodoItem]:
        if expected_owner is not None:
            _validate_owner(expected_owner)
        with self._lock:
            self._items = self._load()
            item = self._find_by_id(todo_id)
            if item is None or (expected_owner is not None and item.owner != expected_owner):
                return None
            self._items.remove(item)
            self._save()
            logger.info("[TodoStore] delete_by_id id=%s", todo_id)
            return item

    def clear_done(self, owner: Optional[str] = None) -> int:
        if owner is not None:
            _validate_owner(owner)
        with self._lock:
            self._items = self._load()
            before = len(self._items)
            self._items = [t for t in self._items if not (t.done and (owner is None or t.owner == owner))]
            removed = before - len(self._items)
            if removed:
                self._save()
            return removed

    def get_all(self) -> List[TodoItem]:
        with self._lock:
            self._items = self._load()
            return list(self._items)
