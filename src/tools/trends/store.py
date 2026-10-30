"""TrendStore — 趋势条目缓存（per-profile）。

存储：profiles/{profile_id}/trend_cache.json

保留策略：
  - 未使用条目最多 20 条（超出时丢弃最旧）
  - 已使用条目保留 3 天后清理
"""
import json
import os
import time
import uuid
from datetime import datetime
from typing import List, Dict, Optional

_MAX_UNUSED_DEFAULT = 20
_USED_RETAIN_DAYS = 3


class TrendItem:
    def __init__(self, d: dict):
        self.id = d.get("id") or str(uuid.uuid4())
        self.title = d.get("title", "")
        self.snippet = d.get("snippet", "")
        self.source_label = d.get("source_label", "")
        self.fetched_at = d.get("fetched_at") or datetime.now().isoformat()
        self.used = bool(d.get("used", False))
        self.used_at = d.get("used_at")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "snippet": self.snippet,
            "source_label": self.source_label,
            "fetched_at": self.fetched_at,
            "used": self.used,
            "used_at": self.used_at,
        }


class TrendStore:
    def __init__(self, storage_root: str):
        self._path = os.path.join(storage_root, "trend_cache.json")
        self._items: List[TrendItem] = self._load()
        self.prune_old_used()

    def _load(self) -> List[TrendItem]:
        try:
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)
                return [TrendItem(d) for d in data if isinstance(d, dict)]
        except Exception:
            pass
        return []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump([i.to_dict() for i in self._items], f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            pass

    def add_items(
        self,
        items: List[Dict],
        max_unused: int = _MAX_UNUSED_DEFAULT,
        source_quota: int = 0,
    ) -> int:
        """添加新条目，按 title 去重，维持 max_unused 全局上限。返回实际添加数量。

        source_quota: 本次调用最多添加几条（per-source 配额，0=不限）。
        """
        existing_titles = {i.title.strip().lower() for i in self._items}
        added = 0
        for d in items:
            if source_quota and added >= source_quota:
                break
            title = (d.get("title") or "").strip()
            if not title or title.lower() in existing_titles:
                continue
            self._items.append(TrendItem(d))
            existing_titles.add(title.lower())
            added += 1

        # 裁剪：未使用条目超出全局上限时按 fetched_at 丢弃最旧的
        unused = [i for i in self._items if not i.used]
        if len(unused) > max_unused:
            unused_sorted = sorted(unused, key=lambda x: x.fetched_at)
            to_remove = {i.id for i in unused_sorted[:len(unused) - max_unused]}
            self._items = [i for i in self._items if i.id not in to_remove]

        if added:
            self._save()
        return added

    def clear_all(self):
        """清空所有缓存条目（含已读和未读）。"""
        self._items = []
        self._save()

    def get_unused(self, n: int = 5) -> List[TrendItem]:
        """返回 n 条未使用条目并标记为 used（供 ReflectionEngine 消费）。"""
        unused = [i for i in self._items if not i.used]
        selected = unused[:n]
        now = datetime.now().isoformat()
        for item in selected:
            item.used = True
            item.used_at = now
        if selected:
            self._save()
        return selected

    def get_recent_raw(self, n: int = 10) -> List[TrendItem]:
        """只读：返回最近 n 条按 fetched_at 倒序，不标记 used（供 TrendAwarenessSegment 使用）。"""
        all_items = sorted(self._items, key=lambda x: x.fetched_at, reverse=True)
        return all_items[:n]

    def get_unused_raw(self, n: int = 5) -> List[TrendItem]:
        """只读：返回 n 条未使用条目，不标记 used（供话题候选列举使用）。"""
        return [i for i in self._items if not i.used][:n]

    def get_by_id(self, item_id: str) -> Optional[TrendItem]:
        """按 id 查找条目；不存在返回 None。"""
        for i in self._items:
            if i.id == item_id:
                return i
        return None

    def mark_used(self, item_id: str) -> bool:
        """按 id 把条目标记为 used（供话题被 ASE 采用后调用）。"""
        for i in self._items:
            if i.id == item_id and not i.used:
                i.used = True
                i.used_at = datetime.now().isoformat()
                self._save()
                return True
        return False

    def count_unused(self) -> int:
        return sum(1 for i in self._items if not i.used)

    def last_fetched_at(self) -> Optional[str]:
        """返回最新条目的 fetched_at，未有数据时返回 None。"""
        if not self._items:
            return None
        return max(i.fetched_at for i in self._items)

    def prune_old_used(self):
        """清理超过 _USED_RETAIN_DAYS 天的已用条目。"""
        cutoff = time.time() - _USED_RETAIN_DAYS * 86400
        before = len(self._items)
        self._items = [
            i for i in self._items
            if not i.used or i.used_at is None or _iso_to_ts(i.used_at) > cutoff
        ]
        if len(self._items) != before:
            self._save()


def _iso_to_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0
