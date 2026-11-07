"""长期事实存储 — profiles/{id}/long_term_facts.json

事实原文和旧版本保留，日常使用按有效期过滤。
"""
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields
from typing import List, Optional
from src.utils.memory_time import has_floating_date

logger = logging.getLogger(__name__)

_WRITE_LOCK = threading.Lock()
_LEGACY_ADDRESS = re.compile(
    r"^用户.{0,10}(?:称呼|叫)(?:\s*AI\s*|你|我|角色)(?:为|作|做)?\s*\S"
    r"|^(?:the )?user.{0,26}(?:calls?|address(?:es)?) (?:the ai|you|the character)\b",
    re.IGNORECASE,
)


@contextmanager
def _fact_file_lock(path: str):
    """Serialize the disk revision check and atomic replace across processes."""
    with open(path + ".lock", "a+b") as lock:
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        # The OS releases the advisory lock when the handle closes.
        yield


def _disk_stamp(stat):
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


_VALID_CATEGORIES = frozenset({
    "habit", "preference", "taboo", "relationship",
    "location", "milestone", "ai_insight", "other",
})
_VALID_SOURCES = frozenset({"manual", "auto", "reflection"})
EXPERIENCE_CONTENT_LIMIT = 1200


def _valid_source_offsets(data):
    if not isinstance(data, dict):
        return {}
    return {
        source: {key: value[key] for key in ("id", "offset", "content_hash")}
        for source, value in data.items()
        if isinstance(source, str) and source and isinstance(value, dict)
        and type(value.get("id")) is int and value["id"] > 0
        and type(value.get("offset")) is int and value["offset"] >= 0
        and isinstance(value.get("content_hash"), str) and value["content_hash"]
    }


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

    kind: str = ""
    event_date: Optional[str] = None
    expires_at: Optional[float] = None
    evidence: List[dict] = field(default_factory=list)
    versions: List[dict] = field(default_factory=list)
    archived: bool = False
    archive_reason: str = ""
    merged_into: Optional[str] = None
    merged_source_version: str = ""
    merged_target_version: str = ""
    archive_revision: str = ""
    archive_note: str = ""
    review_note: str = ""
    curation_protected: bool = False
    occurred_from: Optional[str] = None
    occurred_to: Optional[str] = None
    retention_reason: str = ""

    @property
    def record_version(self) -> int:
        return len(self.versions) + 1

    def snapshot(self) -> dict:
        values = {item.name: getattr(self, item.name) for item in fields(self) if item.name != "versions"}
        return {**deepcopy(values), "record_version": self.record_version}

    def is_active(self, now: Optional[float] = None) -> bool:
        return not self.archived and (
            self.kind == "experience" or self.expires_at is None
            or self.expires_at > (time.time() if now is None else now)
        )

    def score(self) -> float:
        """排序得分：weight * 0.7 + recency * 0.3"""
        hours = (time.time() - self.updated_at) / 3600.0
        recency = 1.0 / (1.0 + math.log1p(hours))
        return self.weight * 0.7 + recency * 0.3

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.kind == "experience":
            data["record_version"] = self.record_version
        else:
            # Keep unchanged legacy records' serialized shape and curation fingerprints.
            for name in ("occurred_from", "occurred_to", "retention_reason"):
                if not data[name]:
                    data.pop(name)
        return data

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
            kind=d.get("kind") or "",
            event_date=d.get("event_date"),
            expires_at=d.get("expires_at") if type(d.get("expires_at")) in (int, float) else None,
            evidence=list(d.get("evidence") or []),
            versions=list(d.get("versions") or []),
            archived=bool(d.get("archived", False)),
            archive_reason=d.get("archive_reason") or "",
            merged_into=d.get("merged_into") or None,
            merged_source_version=d.get("merged_source_version") or "",
            merged_target_version=d.get("merged_target_version") or "",
            archive_revision=d.get("archive_revision") or "",
            archive_note=d.get("archive_note") or "",
            review_note=d.get("review_note") or "",
            curation_protected=bool(d.get("curation_protected", False)),
            occurred_from=d.get("occurred_from"),
            occurred_to=d.get("occurred_to"),
            retention_reason=d.get("retention_reason") or "",
        )


class LongTermStore:
    """持久化事实库，原子写入，永久保存。"""

    def __init__(self, storage_root: str):
        self._path = os.path.join(storage_root, "long_term_facts.json")
        self._facts: List[LongTermFact] = []
        self._cursors: dict[str, int] = {}
        self._offsets: dict[str, dict] = {}
        self._curation: dict = {}
        self._revision = 0
        self._disk_stamp = None
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def add(self, content: str, category: str = "other", weight: float = 1.0,
            source: str = "manual", tags: Optional[List[str]] = None,
            emotional_note: str = "", *,
            updated_at: Optional[float] = None) -> Optional[LongTermFact]:
        """添加新事实。重复时 skip（不修改、不删除已有事实），返回 None。
        updated_at: 可选，合并摘要时传入被合并事实的最旧时间以保持 recency 语义。"""
        self._refresh()
        content = content.strip()
        if not content:
            return None
        if source != 'manual' and has_floating_date(content):
            logger.warning('[LongTermStore] Auto fact has an unresolved relative date; keeping existing memories')
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
        if not self._commit([*self._facts, fact], self._cursors):
            return None
        logger.info("[LongTermStore] 新增事实 id=%s category=%s source=%s", fact.id, fact.category, fact.source)
        return fact

    def update(self, fact_id: str, **kwargs) -> bool:
        """更新事实字段。返回是否成功。
        只有 content / category 变更才刷新 updated_at（影响 score 衰减）。
        pinned / weight / tags 为元数据，不影响时间戳。
        """
        self._refresh()
        fact = self._get_by_id(fact_id)
        if not fact:
            return False
        content = kwargs.get("content")
        if fact.kind == "experience" and content and len(str(content).strip()) > EXPERIENCE_CONTENT_LIMIT:
            return False
        previous = fact
        fact = deepcopy(fact)
        semantic_changed = False
        for k, v in kwargs.items():
            if k == "content" and v:
                fact.content = str(v).strip()
                semantic_changed = True
            elif k == "category" and v in _VALID_CATEGORIES:
                fact.category = v
                semantic_changed = True
            elif k == "weight":
                # 4 位小数：每日衰减 0.998 的单步变化约 0.002，2 位精度会把它整个舍掉
                fact.weight = round(float(v), 4)
            elif k == "tags" and isinstance(v, list):
                fact.tags = v
            elif k == "pinned":
                fact.pinned = bool(v)
        if semantic_changed:
            if fact.kind == "experience":
                fact.versions.append(previous.snapshot())
            fact.updated_at = time.time()
            fact.curation_protected = True
            fact.review_note = ""
        if fact.archived and fact != self._get_by_id(fact_id):
            fact.archive_revision = uuid.uuid4().hex
        return self._commit([fact if f.id == fact_id else f for f in self._facts], self._cursors)

    def delete(self, fact_id: str) -> bool:
        """按 ID 删除事实（唯一删除入口）。"""
        self._refresh()
        before = len(self._facts)
        remaining = [f for f in self._facts if f.id != fact_id]
        if len(remaining) < before and self._commit(remaining, self._cursors):
            logger.info("[LongTermStore] 删除事实 id=%s", fact_id)
            return True
        return False

    def get_all(self) -> List[LongTermFact]:
        self._refresh()
        return list(self._facts)

    def get_active(self, now: Optional[float] = None) -> List[LongTermFact]:
        self._refresh()
        now = time.time() if now is None else now
        return [fact for fact in self._facts if fact.is_active(now)]

    def get_recallable(self, now: Optional[float] = None) -> List[LongTermFact]:
        active = self.get_active(now)
        if not any(f.kind == "address" for f in active):
            return active
        return [f for f in active if f.kind or f.source != "auto" or f.is_manual or f.pinned
                or not _LEGACY_ADDRESS.search(f.content)]

    def source_cursors(self) -> dict[str, int]:
        self._refresh()
        return dict(self._cursors)

    def extraction_snapshot(self):
        revision, cursors, facts, _ = self.extraction_progress_snapshot()
        return revision, cursors, facts

    def apply_extraction(self, revision: int, facts: List[LongTermFact], cursors: dict[str, int]) -> bool:
        return self._commit(facts, cursors, expected_revision=revision)

    def extraction_progress_snapshot(self):
        self._refresh()
        with _WRITE_LOCK:
            return self._revision, dict(self._cursors), deepcopy(self._facts), deepcopy(self._offsets)

    def apply_extraction_progress(self, revision: int, facts: List[LongTermFact],
                                  cursors: dict[str, int], offsets: dict[str, dict]) -> bool:
        validated = _valid_source_offsets(offsets)
        if validated != offsets:
            return False
        return self._commit(facts, cursors, expected_revision=revision, offsets=validated)

    def curation_snapshot(self):
        revision, cursors, facts, state, _ = self.curation_progress_snapshot()
        return revision, cursors, facts, state

    def curation_progress_snapshot(self):
        self._refresh()
        with _WRITE_LOCK:
            return (self._revision, dict(self._cursors), deepcopy(self._facts),
                    deepcopy(self._curation), deepcopy(self._offsets))

    def curation_status(self) -> dict:
        self._refresh()
        with _WRITE_LOCK:
            daily = self._curation.get("daily")
            return {
                "latest": deepcopy(self._curation.get("latest")),
                "index_pending": bool(self._curation.get("index_cleanup_pending")),
                "daily": {"day": daily["day"], "status": daily["status"]} if daily else None,
            }

    def curation_index_token(self) -> Optional[str]:
        self._refresh()
        with _WRITE_LOCK:
            return self._curation.get("index_cleanup_pending")

    def mark_curation_index_clean(self, token: str) -> bool:
        revision, cursors, facts, state = self.curation_snapshot()
        if state.get("index_cleanup_pending") != token:
            return False
        state.pop("index_cleanup_pending")
        return self.apply_curation(revision, facts, cursors, state)

    def apply_curation(self, revision: int, facts: List[LongTermFact], cursors: dict[str, int], state: dict) -> bool:
        return self._commit(facts, cursors, expected_revision=revision, curation=state)

    def apply_curation_progress(self, revision: int, facts: List[LongTermFact],
                                cursors: dict[str, int], offsets: dict[str, dict], state: dict) -> bool:
        validated = _valid_source_offsets(offsets)
        if validated != offsets:
            return False
        return self._commit(facts, cursors, expected_revision=revision, curation=state, offsets=validated)

    def get_top_n(self, n: int) -> List[LongTermFact]:
        """返回 prompt 用事实：全部 pinned + 按 score 降序填充剩余配额。"""
        active = self.get_active()
        pinned = [f for f in active if f.pinned]
        non_pinned = sorted(
            (f for f in active if not f.pinned),
            key=lambda f: f.score(), reverse=True
        )
        remaining = max(0, n - len(pinned))
        return pinned + non_pinned[:remaining]

    def count(self) -> int:
        self._refresh()
        return len(self._facts)

    def clear_all(self) -> int:
        """清空所有事实，返回删除数量。"""
        self._refresh()
        n = len(self._facts)
        if not self._commit([], self._cursors):
            return 0
        logger.info("[LongTermStore] 清空全部事实 (%d 条)", n)
        return n

    def count_by_source(self) -> dict:
        self._refresh()
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
        self._refresh()
        return self._get_by_id(fact_id)

    def _get_by_id(self, fact_id: str) -> Optional[LongTermFact]:
        for f in self._facts:
            if f.id == fact_id:
                return f
        return None

    def _refresh(self):
        try:
            current = _disk_stamp(os.stat(self._path))
        except FileNotFoundError:
            current = None
        if current != self._disk_stamp:
            with _WRITE_LOCK:
                self._load()

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                stamp = _disk_stamp(os.fstat(fp.fileno()))
            facts = [LongTermFact.from_dict(d) for d in data.get("facts", [])]
            offsets = _valid_source_offsets(data.get("source_offsets"))
            curation = data.get("curation")
            curation = curation if isinstance(curation, dict) else {}
            cursors = {key: value for key, value in (data.get("source_cursors") or {}).items()
                       if type(value) is int and value >= 0}
        except FileNotFoundError:
            facts, cursors, offsets, curation, stamp = [], {}, {}, {}, None
        except (OSError, ValueError, TypeError, AttributeError) as error:
            logger.error("[LongTermStore] Could not load facts: %s", error)
            return
        self._facts, self._cursors, self._offsets, self._curation, self._disk_stamp = (
            facts, cursors, offsets, curation, stamp
        )
        self._revision += 1

    def _commit(self, facts: List[LongTermFact], cursors: dict[str, int],
                *, expected_revision: Optional[int] = None, curation: Optional[dict] = None,
                offsets: Optional[dict] = None) -> bool:
        """Atomically replace only the version this store instance loaded."""
        with _WRITE_LOCK:
            if expected_revision is not None and self._revision != expected_revision:
                return False
            return self._replace_file(facts, cursors, curation, offsets)

    def _replace_file(self, facts: List[LongTermFact], cursors: dict[str, int],
                      curation: Optional[dict] = None, offsets: Optional[dict] = None) -> bool:
        curation = self._curation if curation is None else curation
        offsets = self._offsets if offsets is None else offsets
        tmp = f"{self._path}.{uuid.uuid4().hex}.tmp"
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with _fact_file_lock(self._path):
                stamp = self._write_current_file(tmp, facts, cursors, curation, offsets)
                if stamp is None:
                    return False
        except (OSError, TypeError, ValueError) as error:
            logger.error("[LongTermStore] Could not save facts: %s", error)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return False
        self._disk_stamp = stamp
        self._facts = facts
        self._cursors = dict(cursors)
        self._offsets = deepcopy(offsets)
        self._curation = deepcopy(curation)
        self._revision += 1
        return True

    def _write_current_file(self, tmp, facts, cursors, curation, offsets):
        try:
            current = _disk_stamp(os.stat(self._path))
        except FileNotFoundError:
            current = None
        if current != self._disk_stamp:
            logger.warning("[LongTermStore] Fact file changed since this store was loaded")
            return None
        with open(tmp, "w", encoding="utf-8") as fp:
            payload = {"facts": [f.to_dict() for f in facts], "source_cursors": cursors}
            if offsets:
                payload["source_offsets"] = offsets
            if curation:
                payload["curation"] = curation
            json.dump(payload, fp, ensure_ascii=False, indent=2)
            fp.flush()
            stamp = _disk_stamp(os.fstat(fp.fileno()))
        os.replace(tmp, self._path)
        return stamp
