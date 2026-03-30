"""长期事实存储 — profiles/{id}/long_term_facts.json

事实永久保存，无 TTL，无自动删除：
  - add() 发现重复时跳过，不修改任何已有事实
  - get_top_n() 只排序切片，不删除
  - 唯一删除入口：delete(fact_id)，由用户或命令系统显式调用
"""
import json
import logging
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

_VALID_CATEGORIES = frozenset({
    "habit", "preference", "taboo", "relationship",
    "location", "milestone", "ai_insight", "other",
})
_VALID_SOURCES = frozenset({"manual", "auto", "reflection"})


@dataclass
class LongTermFact:
    content: str
    id: str = field(default_factory=lambda: "f_" + uuid.uuid4().hex[:8])
    weight: float = 1.0
    source: str = "manual"        # manual | auto | reflection
    is_manual: bool = True
    category: str = "other"       # habit|preference|taboo|relationship|location|milestone|ai_insight|other
    updated_at: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)
    pinned: bool = False
    emotional_note: str = ""     # AI 对这条事实的情感注记，如"令人担心"，提取时生成，可为空

    def score(self) -> float:
        """排序得分：weight * 0.7 + recency * 0.3"""
        hours = (time.time() - self.updated_at) / 3600.0
        recency = 1.0 / (1.0 + math.log1p(hours))
        return self.weight * 0.7 + recency * 0.3

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LongTermFact":
        return cls(
            id=d.get("id", "f_" + uuid.uuid4().hex[:8]),
            content=d.get("content", ""),
            weight=round(float(d.get("weight", 1.0)), 2),
            source=d.get("source", "manual"),
            is_manual=bool(d.get("is_manual", True)),
            category=d.get("category", "other"),
            updated_at=float(d.get("updated_at", time.time())),
            tags=d.get("tags", []),
            pinned=bool(d.get("pinned", False)),
            emotional_note=str(d.get("emotional_note", "")),
        )


class LongTermStore:
    """持久化事实库，原子写入，永久保存。"""

    def __init__(self, storage_root: str):
        self._path = os.path.join(storage_root, "long_term_facts.json")
        self._facts: List[LongTermFact] = []
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def add(self, content: str, category: str = "other", weight: float = 1.0,
            source: str = "manual", tags: Optional[List[str]] = None,
            emotional_note: str = "", *,
            updated_at: Optional[float] = None) -> Optional[LongTermFact]:
        """添加新事实。重复时 skip（不修改、不删除已有事实），返回 None。
        updated_at: 可选，合并摘要时传入被合并事实的最旧时间以保持 recency 语义。"""
        content = content.strip()
        if not content:
            return None
        if self._is_duplicate(content):
            logger.debug("[LongTermStore] 跳过重复事实: %.40s", content)
            return None

        fact = LongTermFact(
            content=content,
            weight=round(float(weight), 2),
            source=source if source in _VALID_SOURCES else "auto",
            is_manual=(source == "manual"),
            category=category if category in _VALID_CATEGORIES else "other",
            tags=tags or [],
            emotional_note=emotional_note[:15] if emotional_note else "",  # cap to prevent runaway output
        )
        if updated_at is not None:
            fact.updated_at = float(updated_at)
        self._facts.append(fact)
        self._save()
        logger.info("[LongTermStore] 新增事实 id=%s category=%s source=%s", fact.id, fact.category, fact.source)
        return fact

    def update(self, fact_id: str, **kwargs) -> bool:
        """更新事实字段。返回是否成功。
        只有 content / category 变更才刷新 updated_at（影响 score 衰减）。
        pinned / weight / tags 为元数据，不影响时间戳。
        """
        fact = self._get_by_id(fact_id)
        if not fact:
            return False
        semantic_changed = False
        for k, v in kwargs.items():
            if k == "content" and v:
                fact.content = str(v).strip()
                semantic_changed = True
            elif k == "category" and v in _VALID_CATEGORIES:
                fact.category = v
                semantic_changed = True
            elif k == "weight":
                fact.weight = round(float(v), 2)
            elif k == "tags" and isinstance(v, list):
                fact.tags = v
            elif k == "pinned":
                fact.pinned = bool(v)
        if semantic_changed:
            fact.updated_at = time.time()
        self._save()
        return True

    def delete(self, fact_id: str) -> bool:
        """按 ID 删除事实（唯一删除入口）。"""
        before = len(self._facts)
        self._facts = [f for f in self._facts if f.id != fact_id]
        if len(self._facts) < before:
            self._save()
            logger.info("[LongTermStore] 删除事实 id=%s", fact_id)
            return True
        return False

    def get_all(self) -> List[LongTermFact]:
        return list(self._facts)

    def get_top_n(self, n: int) -> List[LongTermFact]:
        """返回 prompt 用事实：全部 pinned + 按 score 降序填充剩余配额。"""
        pinned = [f for f in self._facts if f.pinned]
        non_pinned = sorted(
            (f for f in self._facts if not f.pinned),
            key=lambda f: f.score(), reverse=True
        )
        remaining = max(0, n - len(pinned))
        return pinned + non_pinned[:remaining]

    def count(self) -> int:
        return len(self._facts)

    def clear_all(self) -> int:
        """清空所有事实，返回删除数量。"""
        n = len(self._facts)
        self._facts = []
        self._save()
        logger.info("[LongTermStore] 清空全部事实 (%d 条)", n)
        return n

    def count_by_source(self) -> dict:
        result = {"manual": 0, "auto": 0, "reflection": 0}
        for f in self._facts:
            src = f.source if f.source in result else "auto"
            result[src] += 1
        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    def _is_duplicate(self, content: str) -> bool:
        """Jaccard 相似度 > 0.8 视为重复。"""
        tokens_new = set(content.lower().split())
        if not tokens_new:
            return False
        for f in self._facts:
            tokens_old = set(f.content.lower().split())
            if not tokens_old:
                continue
            intersection = len(tokens_new & tokens_old)
            union = len(tokens_new | tokens_old)
            if union > 0 and intersection / union > 0.8:
                return True
        return False

    def get_by_id(self, fact_id: str) -> Optional[LongTermFact]:
        """按 ID 查找事实（公开接口）。"""
        return self._get_by_id(fact_id)

    def _get_by_id(self, fact_id: str) -> Optional[LongTermFact]:
        for f in self._facts:
            if f.id == fact_id:
                return f
        return None

    def _load(self):
        if not os.path.exists(self._path):
            self._facts = []
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            self._facts = [LongTermFact.from_dict(d) for d in data.get("facts", [])]
        except Exception as e:
            logger.error("[LongTermStore] 加载失败 %s: %s", self._path, e)
            self._facts = []

    def _save(self):
        """原子写入（写 .tmp 后 rename）。"""
        tmp = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump({"facts": [f.to_dict() for f in self._facts]}, fp,
                          ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.error("[LongTermStore] 保存失败: %s", e)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
