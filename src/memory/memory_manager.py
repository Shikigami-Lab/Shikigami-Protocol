"""记忆管理器 — 协调 facts / vectors / day_summaries

职责：
  - 合并全局 + profile 级别记忆配置
  - auto_extract: 每 freq 轮触发 LLM 提取事实并写入 facts + vectors
  - generate_day_summary: 生成昨日对话摘要
  - _build_retrieval_query: 三轴向量检索查询
  - status(): 返回当前记忆系统状态
"""
import asyncio
import json
import logging
import math
import os
import time
import traceback
import uuid
from weakref import WeakValueDictionary
from datetime import date, timedelta
from string import Template
from typing import Any, Dict, List, Optional

from src.config.memory_prompts import memory_task_prompt
from src.config.prompt_loader import get_prompt
from src.utils.memory_time import write_time_context

from src.memory.long_term_store import LongTermFact, LongTermStore
from src.memory.day_store import DaySummaryStore
from src.memory.extraction_batches import is_memory_turn as _is_memory_turn, plan_batch
from src.memory.merged_history import memory_group_sources_allowed
from src.memory.vector_store import VectorMemoryStore
from src.memory.bm25_retriever import BM25Retriever
from src.utils.debug_logger import (
    log_secondary_llm_call,
    log_secondary_llm_response,
    log_error,
    log_memory_startup,
    log_summary_generate,
)
from src.memory.fact_operations import (
    EXTRACTION_CANDIDATE_LIMIT, FORMAT, apply_operations, choose_candidates, extraction_candidate_query, rank_related,
)
from src.memory.recall import RecallCandidates, experience_links_by_day, search_vectors
from src.utils.paths import get_project_root
from src.utils.persona_context import persona_prompt_parts

logger = logging.getLogger(__name__)

_PROFILES_DIR = os.path.join(get_project_root(), "profiles")
_FACT_INDEX_LOCKS = WeakValueDictionary()


def _vector_document_for_fact(fact: Any) -> str:
    """用于向量嵌入的文本：事实正文 + tags，使语义检索与阈值同时考虑正文与关键词。"""
    content = (fact.content or "").strip()
    tags = getattr(fact, "tags", None) or []
    if tags:
        content = content + " [关键词: " + " ".join(str(t) for t in tags) + "]"
    return content


def _vector_metadata_for_fact(fact: Any, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从 LongTermFact 构建写入向量库的 metadata。不含 weight/pinned（未被读取）。"""
    meta: Dict[str, Any] = {
        "fact_id": fact.id,
        "category": fact.category,
        "created_at": fact.updated_at,
        "emotional_note": (fact.emotional_note or "")[:64],
    }
    if extra:
        meta.update(extra)
    return meta


class MemoryManager:

    def __init__(self, profile_id: str, storage_root: str,
                 profile_memory_cfg: Optional[Dict[str, Any]] = None,
                 app_memory_cfg: Optional[Dict[str, Any]] = None):
        self._profile_id = profile_id
        self._storage_root = storage_root

        # 合并配置：profile 覆盖全局，全局覆盖默认
        app_cfg = app_memory_cfg or {}
        self._global_memory_cfg = app_cfg
        prof_cfg = profile_memory_cfg or {}
        self._cfg: Dict[str, Any] = {
            "enabled": False,
            "vector_enabled": False,
            "extraction_frequency": 5,
            "extraction_weight_threshold": 0.5,
            "max_facts_in_prompt": 8,
            "day_summary_enabled": False,
            "day_summary_keep_days": 14,
            # 每日遗忘（只跑已加载且昨日有对话的 session）
            "daily_forgetting_enabled": False,
            "daily_decay_factor": 0.998,
            "daily_decay_min_weight": 0.1,
            "daily_reinforcement_enabled": False,
            "daily_run_at_hour": 0,
            "daily_run_at_minute": 5,
            "daily_consolidation_enabled": False,
            "daily_consolidation_after_days": 30,
            "daily_consolidation_weight_below": 0.3,
            "daily_consolidation_batch_max": 15,
            # 检索槽位配额（可在 app.yaml / profile memory_config 中覆盖）
            "recent_fact_quota": 3,      # 近期槽位最多条数
            "semantic_fact_quota": 2,    # 语义槽位最多条数
            "recent_days": 3,            # 多少天以内算"近期"（facts 槽位共用）
            "semantic_distance": 0.45,   # facts 语义槽的 distance 阈值
            "embedding": {
                "provider": "gemini",
                "gemini_model": "gemini-embedding-001",
                "local_model": "all-MiniLM-L6-v2",
            },
        }
        self._cfg.update(app_cfg)
        # profile 级别配置逐字段覆盖（仅覆盖非空值）
        for k, v in prof_cfg.items():
            if k == "embedding" and isinstance(v, dict):
                self._cfg["embedding"] = {**self._cfg["embedding"], **v}
            elif v is not None:
                self._cfg[k] = v

        # 初始化各子存储
        self.facts = LongTermStore(storage_root)
        self.day_store = DaySummaryStore(storage_root)

        # Search is optional. The old index can still be migrated and cleaned locally.
        memory_enabled = self._cfg.get("enabled")
        self.vectors: Optional[VectorMemoryStore] = None
        if memory_enabled and self._cfg.get("vector_enabled"):
            embed_cfg = self._cfg.get("embedding", {})
            self.vectors = VectorMemoryStore(storage_root, embed_cfg)
        self._offline_index: Optional[VectorMemoryStore] = None
        if memory_enabled and not (self.vectors and self.vectors.is_available()) and os.path.isfile(
            os.path.join(storage_root, "chroma_db", "chroma.sqlite3"),
        ):
            self._offline_index = VectorMemoryStore(storage_root, {}, existing_only=True)

        # BM25 关键词检索（可选依赖 rank_bm25，缺失时静默降级）
        self.bm25 = BM25Retriever()

        # turn_counter（用于 auto_extract 频率控制）
        self._meta_path = os.path.join(storage_root, "memory_meta.json")
        self._extraction_lock = asyncio.Lock()
        index_path = os.path.normcase(os.path.realpath(storage_root))
        self._fact_index_lock = _FACT_INDEX_LOCKS.setdefault(index_path, asyncio.Lock())
        if self.fact_index:
            self.import_legacy_vectors()

        # 启动日志
        vec_ok = bool(self.vectors and self.vectors.is_available())
        vec_count = self.vectors.count() if vec_ok else 0
        vec_provider = getattr(self.vectors, "_actual_provider_name",
                               self.vectors._embed_config.get("provider", "none")) if self.vectors else "none"
        logger.info(
            "[MemoryManager] 初始化完成 profile=%s facts=%d vectors=%d(%s) summaries=%d",
            profile_id, self.facts.count(), vec_count, vec_provider, self.day_store.count()
        )
        log_memory_startup(
            profile_id=profile_id,
            facts_count=self.facts.count(),
            vector_ok=vec_ok,
            vector_count=vec_count,
            vector_provider=vec_provider,
            summary_count=self.day_store.count(),
        )

    # ── 公共 API ──────────────────────────────────────────────────────────────

    def _expand_card_macros(self, text: str, profile: Dict = None, app=None) -> str:
        """Expand card macros in the shared persona and the configured task instruction."""
        if not text or ("{{" not in text and "<" not in text):
            return text
        from src.core.user_portrait import _resolve_user_intro
        from src.prompt.macros import build_macro_table, expand_macros
        from src.utils.persona_context import resolve_user_name
        profile = profile or {}
        _, introduction = _resolve_user_intro(profile, app)
        return expand_macros(text, build_macro_table(
            char_name=profile.get("display_name") or profile.get("name") or "",
            user_name=resolve_user_name(profile, app) or "用户",
            persona=introduction,
        ))

    def status(self) -> Dict[str, Any]:
        vec_ok = bool(self.vectors and self.vectors.is_available())
        vec_count = len(self.current_vector_rows(self.vectors.get_all())) if vec_ok else 0
        vec_provider = (getattr(self.vectors, "_actual_provider_name",
                                self.vectors._embed_config.get("provider", "none"))
                        if self.vectors else "none")
        src_counts = self.facts.count_by_source()
        return {
            "enabled": bool(self._cfg.get("enabled")),
            "facts_count": self.facts.count(),
            "facts_by_source": src_counts,
            "vector_ok": vec_ok,
            "vector_count": vec_count,
            "vector_provider": vec_provider,
            "summary_count": self.day_store.count(),
            "day_summary_enabled": bool(self._cfg.get("enabled") and self._cfg.get("day_summary_enabled")),
        }

    def get_reflection_context_facts(
        self,
        max_count: int = 4,
        min_weight: float = 0.6,
        query: str = "",
    ) -> List[Dict[str, Any]]:
        """返回适合注入 Reflection 的高权重长期事实，供记忆引用话题。

        筛选：weight >= min_weight，优先 emotional_note 非空（情感相关），
        按 weight 降序再按 updated_at 降序。只返回 content/weight/updated_at 字段。
        """
        if not self._cfg.get("enabled"):
            return []
        from datetime import datetime
        if query and max_count > 0:
            pool = RecallCandidates(self.facts)
            all_facts = pool.active.values()
            relevant = [fact for fact in all_facts if fact.kind == "experience"]
            related_cold = rank_related([*relevant, *pool.cold.values()], query, 1)
        else:
            all_facts = self.facts.get_recallable()
            related_cold = []
        filtered = [f for f in all_facts if f.kind != "experience" and f.weight >= min_weight]
        # 有情感注记的排前面
        filtered.sort(
            key=lambda f: (
                bool(getattr(f, "emotional_note", None)),
                f.weight,
                f.updated_at,
            ),
            reverse=True,
        )
        result = []
        for f in [*related_cold, *filtered[:max_count - len(related_cold)]]:
            dt = datetime.fromtimestamp(f.updated_at).strftime("%Y-%m-%d")
            result.append({
                "content": f.content,
                "weight": round(f.weight, 2),
                "date": dt,
            })
        return result

    @property
    def fact_index(self) -> Optional[VectorMemoryStore]:
        for index in (self.vectors, self._offline_index):
            if index and index.is_available():
                return index
        return None

    def import_legacy_vectors(self) -> None:
        """Save vector-only notes as manual facts before attaching their existing index rows."""
        index = self.fact_index
        if index is None:
            return
        try:
            legacy = [row for row in index.get_all(strict=True)
                      if "fact_id" not in (row.get("metadata") or {})]
            if not legacy:
                return
            revision, cursors, facts, state = self.facts.curation_snapshot()
            if state.get("legacy_vectors_cleared"):
                for row in legacy:
                    index.delete(row["id"])
                return
            legacy = [row for row in legacy if (row.get("document") or "").strip()]
            imported = state.setdefault("legacy_vector_imports", {})
            pool = RecallCandidates(self.facts)
            by_text = {fact.content.strip(): fact for fact in pool.active.values()}
            forgotten_texts = {fact.content.strip() for fact in facts} - by_text.keys()
            links = {}
            for row in legacy:
                vector_id = row["id"]
                if vector_id not in imported:
                    text = row["document"].strip()
                    fact = by_text.get(text)
                    if fact is None:
                        forgotten = text in forgotten_texts
                        fact = LongTermFact(
                            content=text, archived=forgotten,
                            archive_reason="legacy_duplicate" if forgotten else "",
                            archive_revision=uuid.uuid4().hex if forgotten else "",
                        )
                        created = (row.get("metadata") or {}).get("created_at")
                        if type(created) in (float, int) and math.isfinite(created):
                            fact.updated_at = created
                        facts.append(fact)
                        by_text[text] = fact
                    links[vector_id] = fact.id
            if links:
                imported.update(links)
                if not self.facts.apply_curation(revision, facts, cursors, state):
                    return
            for row in legacy:
                index.link_fact(row["id"], imported[row["id"]])
        except Exception:
            logger.warning("Legacy vector import will be retried for profile %s", self._profile_id)

    def current_vector_rows(self, rows, pool=None) -> list:
        """Keep a single current vector for each recallable fact, in input order."""
        if pool is None:
            pool = RecallCandidates(self.facts)
        seen = set()
        result = []
        for row in rows:
            fact_id = (row.get("metadata") or {}).get("fact_id")
            fact = pool.indexable.get(fact_id)
            if fact and fact_id not in seen and self.fact_vector_matches(fact, row):
                seen.add(fact_id)
                result.append(row)
        return result

    async def sync_facts_to_vectors(self) -> int:
        """Repair the existing index and embed missing recallable facts on request."""
        if not self.vectors or not self.vectors.is_available():
            return 0
        async with self._fact_index_lock:
            await asyncio.to_thread(self.import_legacy_vectors)
            await self._prune_fact_index()
            existing = await asyncio.to_thread(self.vectors.get_all, strict=True)
            pool = RecallCandidates(self.facts)
            indexed = {row["metadata"]["fact_id"] for row in self.current_vector_rows(existing, pool)}
            count = 0
            for fact_id in pool.indexable.keys() - indexed:
                count += int(await self._write_fact_vector(fact_id))
            return count

    def get_current_address_fact(self):
        return next((fact for fact in self.facts.get_active() if fact.kind == "address"), None)

    @staticmethod
    def fact_vector_matches(fact, vector) -> bool:
        document = vector.get("document")
        # Legacy vectors may omit the keyword suffix, but still need current text.
        return (document == _vector_document_for_fact(fact)
                or (not fact.kind and document == fact.content.strip()))

    def _fact_index_cleanup(self, *, strict: bool = False):
        index = self.fact_index
        vectors = index.get_all(strict=strict) if index else []
        pool = RecallCandidates(self.facts)
        expired = sum(not f.archived and f.expires_at is not None and f.expires_at <= pool.now
                      for f in pool.facts)
        # Prefer the stable ID to an older duplicate of the same fact.
        vectors.sort(key=lambda row: row["id"] != (row.get("metadata") or {}).get("fact_id"))
        current = {row["id"] for row in self.current_vector_rows(vectors, pool)}
        clear_legacy = self.facts.legacy_vectors_cleared()
        obsolete = [row["id"] for row in vectors if row["id"] not in current
                    and (clear_legacy or "fact_id" in (row.get("metadata") or {}))]
        return len(pool.active), expired, obsolete

    def preview_fact_index(self) -> dict:
        active, expired, obsolete = self._fact_index_cleanup()
        return {"active": active, "expired": expired, "vectors_removed": len(obsolete)}

    async def maintain_fact_index(self) -> dict:
        """Prune vectors for unavailable or changed facts without requesting embeddings."""
        async with self._fact_index_lock:
            return await self._prune_fact_index()

    async def _prune_fact_index(self) -> dict:
        token = self.facts.curation_index_token()
        index = self.fact_index
        active, expired, obsolete = await asyncio.to_thread(self._fact_index_cleanup, strict=True)
        removed = 0
        for vector_id in obsolete:
            removed += int(bool(await asyncio.to_thread(index.delete, vector_id)))
        if token and index and removed == len(obsolete):
            await asyncio.to_thread(self.facts.mark_curation_index_clean, token)
        return {"active": active, "expired": expired, "vectors_removed": removed}

    async def prune_curation_index(self) -> None:
        if not self.fact_index or not self.facts.curation_index_token():
            return
        try:
            await self.maintain_fact_index()
        except Exception:
            logger.warning("Curation index cleanup will be retried for profile %s", self._profile_id)

    async def choose_extraction_candidates(self, facts: list[LongTermFact], rows: list[Dict]) -> list[LongTermFact]:
        """Choose write candidates from the supplied fact snapshot and dialogue batch."""
        try:
            max_distance = float(self._cfg.get("semantic_distance", 0.45))
        except (TypeError, ValueError, OverflowError):
            return choose_candidates(facts, rows)
        query = extraction_candidate_query(rows)
        if (not facts or not query.strip() or not (0 < max_distance < math.inf)
                or not self.vectors or not self.vectors.is_available()):
            return choose_candidates(facts, rows)
        vector_rows = await asyncio.to_thread(search_vectors, self.vectors, query, EXTRACTION_CANDIDATE_LIMIT)
        return choose_candidates(
            facts, rows, vector_rows=vector_rows,
            vector_matches=lambda fact, row: row.get("document") == _vector_document_for_fact(fact),
            max_distance=max_distance,
        )

    async def auto_extract(self, sources: Dict[str, Any], profile: Dict, app,
                           emotion_context: Optional[Dict] = None, *, first_message=None) -> None:
        """Extract each source's new messages once, committing facts and cursors together."""
        if not self._cfg.get("enabled") or self._cfg.get("extraction_llm_preset") == "__none__":
            return

        async with self._extraction_lock:
            if not sources:
                return
            trigger = first_message[0] if first_message else next(iter(sources))
            frequency = max(1, int(self._cfg.get("extraction_frequency", 5)))
            cursors = self.facts.source_cursors()
            unseen = trigger not in cursors
            due = self._increment_extraction_counter(trigger) % frequency == 0
            if not unseen and not due:
                return

            revision, cursors, facts, offsets = self.facts.extraction_progress_snapshot()
            if trigger not in cursors:
                tail = sources[trigger].memory_tail_id()
                cursors[trigger] = min(tail, first_message[1] - 1) if first_message else tail

            streams = {}
            if due:
                for source, store in sources.items():
                    # Initialize each source on its own callback, after the first reply.
                    if source not in cursors:
                        continue
                    if source == trigger:
                        fresh = store.memory_turn_batch_after(cursors[source], max(20, frequency * 2))
                    else:
                        fresh = store.memory_messages_after(cursors[source], 2)
                    streams[source] = [dict(row, source=source) for row in fresh]

            rows, originals, next_cursors, next_offsets = plan_batch(streams, cursors, offsets)
            if not any(row["role"] == "user" for row in rows):
                if unseen or next_cursors != cursors or next_offsets != offsets:
                    self.facts.apply_extraction_progress(revision, facts, next_cursors, next_offsets)
                return

            if not memory_group_sources_allowed(app, self._profile_id, originals):
                return
            references = {row["reference"]: row for row in rows}
            candidates = await self.choose_extraction_candidates(facts, rows)
            try:
                operations = await self._extract_facts_llm(rows, profile, app, candidates=candidates)
            except Exception:
                logger.warning("[MemoryManager] Extraction failed; pending messages kept (profile=%s)", self._profile_id)
                return

            if not memory_group_sources_allowed(app, self._profile_id, originals):
                return
            if not all(sources[row["source"]].has_memory_evidence(row) for row in originals):
                return
            try:
                changed = apply_operations(facts, operations, references, candidates,
                                           float(self._cfg.get("extraction_weight_threshold", 0.5)))
            except ValueError:
                logger.warning("[MemoryManager] Invalid extraction evidence; pending messages kept")
                return
            if not self.facts.apply_extraction_progress(revision, facts, next_cursors, next_offsets):
                return
            if self.vectors and self.vectors.is_available():
                for fact_id in changed:
                    await self.reembed_fact(fact_id, emotion_context=emotion_context)

    def _memory_system_prompt(self, task: str, profile: Dict, app, protocol: str = "") -> str:
        config = getattr(getattr(app, "state", None), "config", None)
        global_memory = getattr(config, "memory", self._global_memory_cfg)
        persona = self._expand_card_macros("\n\n".join(persona_prompt_parts(profile)), profile, app)
        instruction = self._expand_card_macros(memory_task_prompt(global_memory, task), profile, app)
        return "\n\n".join(part for part in (persona, get_prompt("memory.task_boundary"), instruction, protocol)
                           if part)

    def _build_day_summary_context(self, date_str: str, msg_count: int, profile: Dict, app) -> Dict[str, str]:
        """Collect existing diary continuity and relationship context."""
        # How to refer to the user: a system word like 「用户」 in the prompt pushes the diary toward a
        # report tone, so the default config name counts as unset. Order: the user's own name, a custom
        # config name, how the character usually addresses them, then a neutral word.
        from src.config.prompt_loader import get_locale
        from src.core.user_portrait import _resolve_user_intro, extract_style_address_form
        _cfg2 = getattr(getattr(app, "state", None), "config", None) if app else None
        _configured = str(getattr(_cfg2, "user_name", "") or "").strip() if _cfg2 else ""
        user_name = (
            _resolve_user_intro(profile or {}, app)[0]
            or ("" if _configured.casefold() in {"用户", "user", "the user"} else _configured)
            or extract_style_address_form(profile or {})
            or ("them" if get_locale() == "en" else "对方")
        )

        # Historical backfills must only see diary entries before the date being summarized.
        prior = sorted(
            (entry for entry in self.day_store.get_all() if entry.get("date") and entry["date"] < date_str),
            key=lambda entry: entry["date"],
        )
        recent = prior[-3:]
        if recent:
            recent_summaries = "\n".join(
                f"[{s['date']}] {s['summary']}" for s in recent
            )
        else:
            recent_summaries = "（暂无近期日记）"

        # 好感状态
        affinity_status = ""
        try:
            sm = getattr(getattr(app, "state", None), "session_manager", None)
            session = next(
                (s for s in (sm.list_sessions() if sm else []) if s.profile_id == self._profile_id),
                None
            )
            if session:
                ae = getattr(app.state, "affinity_engine", None)
                if ae:
                    state = ae.load_state(session)
                    affinity_status = state.get("status", "")
        except Exception:
            pass

        # 活跃度对比：与近 7 天平均消息数对比，仅在显著差异时备注
        activity_note = ""
        if msg_count > 0:
            active_days = [entry for entry in prior[-7:] if entry.get("message_count", 0) > 0]
            if len(active_days) >= 2:
                avg = sum(s["message_count"] for s in active_days) / len(active_days)
                if avg > 0:
                    ratio = msg_count / avg
                    if ratio >= 1.6:
                        activity_note = f"（今天聊了 {msg_count} 条，比平时多不少，平均 {avg:.0f} 条）"
                    elif ratio <= 0.4:
                        activity_note = f"（今天只聊了 {msg_count} 条，比平时少很多，平均 {avg:.0f} 条）"

        return {
            "character_name": profile.get("display_name") or profile.get("name") or self._profile_id,
            "user_name": user_name,
            "recent_summaries": recent_summaries,
            "affinity_status": affinity_status,
            "activity_note": ("\n" + activity_note) if activity_note else "",
        }

    async def generate_day_summary(self, date_str: str,
                                   store, profile: Dict, app,
                                   force: bool = False,
                                   ignore_enabled: bool = False,
                                   day_msgs_override: Optional[List] = None,
                                   quiet: bool = False) -> Optional[Dict]:
        """生成指定日期的对话摘要。返回 dict(summary, message_count, truncated) 或 None。

        quiet=True: 当日无对话时仍生成一篇安静日记。
        day_msgs_override: 若传入（如按日合并的单聊+群聊消息列表），则不再从 store 读取，直接用该列表。
        """
        if not self._cfg.get("enabled") or (not ignore_enabled and not self._cfg.get("day_summary_enabled")):
            return None
        if not force and self.day_store.has_summary_for(date_str):
            return None

        logger.info("[MemoryManager] 生成日摘要 date=%s profile=%s quiet=%s", date_str, self._profile_id, quiet)

        def _msg_role(m): return m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        def _msg_content(m): return m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        def _msg_sender(m): return m.get("sender") if isinstance(m, dict) else getattr(m, "sender", "")

        user_name = getattr(getattr(app, "state", None) and getattr(app.state, "config", None), "user_name", "用户") or "用户"
        def _is_human(m): return _msg_role(m) == "user"

        if quiet:
            day_msgs = []
        elif day_msgs_override is not None:
            day_msgs = [m for m in day_msgs_override
                        if _msg_date(m) == date_str and _is_memory_turn(m)]
        else:
            try:
                all_msgs = store.get_all()
            except Exception as e:
                logger.error("[MemoryManager] 读取对话历史失败: %s", e)
                log_error("memory", str(e), {"profile_id": self._profile_id, "op": "read_history"}, traceback_str=traceback.format_exc())
                return None
            day_msgs = [m for m in all_msgs
                        if _msg_date(m) == date_str and _is_memory_turn(m)]

        if not day_msgs and not quiet:
            return None

        total_day = len(day_msgs)
        truncated_msg = False
        truncated_text = False
        conv_text = ""

        if day_msgs:
            max_messages = max(10, min(500, int(self._cfg.get("day_summary_max_messages", 100))))
            day_msgs = day_msgs[:max_messages]
            truncated_msg = total_day > max_messages
            conv_text = "\n".join(
                f"{_msg_sender(m) or ('用户' if _is_human(m) else 'AI')}: {_msg_content(m)}"
                for m in day_msgs
            )
            max_conv_chars = max(2000, min(100000, int(self._cfg.get("day_summary_max_conv_chars", 12000))))
            if len(conv_text) > max_conv_chars:
                conv_text = conv_text[:max_conv_chars] + "\n...（内容过长，已截断）"
                truncated_text = True

        ctx = self._build_day_summary_context(date_str, len(day_msgs), profile, app)

        task_prompt = self._memory_system_prompt("day_summary_prompt", profile, app)
        diary_input = Template(get_prompt("memory.day_summary_input")).safe_substitute(
            date_str=date_str, conv_text=conv_text or get_prompt("memory.day_summary_no_conversation"), **ctx,
        )

        try:
            from src.llm.registry import get_provider
            from src.utils.debug_logger import log_secondary_llm_call, log_secondary_llm_response
            extraction_preset_name = self._cfg.get("extraction_llm_preset", "")
            # 特殊值："__none__"=不生成摘要；""=使用激活模型
            if extraction_preset_name == "__none__":
                return None
            if extraction_preset_name:
                preset = app.state.config.get_llm_preset(extraction_preset_name)
            else:
                preset = app.state.config.get_active_llm_preset()
            llm = get_provider(preset)
            _summary_model = preset.get("model", "unknown") if preset else "unknown"
            _summary_msgs = [
                {"role": "system", "content": task_prompt},
                {"role": "user", "content": diary_input},
            ]
            log_secondary_llm_call(
                role="memory_summary",
                messages=_summary_msgs,
                model=_summary_model,
                gen_kwargs={},
                session_id=self._profile_id,
            )
            _t0 = time.time()
            summary = ""
            async for token in llm.stream_chat(_summary_msgs):
                summary += token
            log_secondary_llm_response(
                role="memory_summary",
                response=summary,
                model=_summary_model,
                session_id=self._profile_id,
                duration_ms=int((time.time() - _t0) * 1000),
            )
            summary = summary.strip()
            if summary and not summary.startswith("[LLM Error:"):
                if truncated_msg or truncated_text:
                    notes = []
                    if truncated_msg:
                        notes.append(f"（本日共{total_day}条对话，摘要基于前{max_messages}条）")
                    if truncated_text:
                        notes.append("（对话内容过长已截断）")
                    summary = summary + " " + " ".join(notes)
                keep_days = int(self._cfg.get("day_summary_keep_days", 14))
                self.day_store.add_summary(date_str, summary,
                                           message_count=len(day_msgs),
                                           keep_days=keep_days,
                                           experience_ids=sorted(experience_links_by_day(self.facts.get_all()).get(date_str, ())))
                logger.info("[MemoryManager] 日摘要已生成 date=%s len=%d profile=%s",
                            date_str, len(summary), self._profile_id)
                log_summary_generate(self._profile_id, date_str, len(day_msgs), summary)
                return {
                    "summary": summary,
                    "message_count": len(day_msgs),
                    "truncated": truncated_msg or truncated_text,
                }
        except Exception as e:
            logger.error("[MemoryManager] 生成日摘要失败: %s", e)
            log_error("memory", str(e), {"profile_id": self._profile_id, "op": "day_summary"}, traceback_str=traceback.format_exc())
        return None

    async def reembed_fact(self, fact_id: str, emotion_context: Optional[Dict] = None) -> bool:
        """Refresh a fact's unique vector, or drop it when the fact is no longer recallable."""
        if not (self.vectors and self.vectors.is_available()):
            if self.fact_index:
                try:
                    await self.maintain_fact_index()
                except Exception:
                    logger.warning("Offline index cleanup will be retried for profile %s", self._profile_id)
            return False
        async with self._fact_index_lock:
            return await self._write_fact_vector(fact_id, emotion_context)

    async def _write_fact_vector(self, fact_id: str, emotion_context: Optional[Dict] = None) -> bool:
        try:
            fact = RecallCandidates(self.facts).indexable.get(fact_id)
            if fact is None:
                await asyncio.to_thread(self.vectors.delete_by_fact_ids, [fact_id])
                return False
            extra = {}
            if emotion_context:
                extra["ai_emotion"] = emotion_context.get("primary_emotion", "")
                extra["ai_energy"] = emotion_context.get("energy_level", 80.0)
            document = _vector_document_for_fact(fact)
            saved = await asyncio.to_thread(self.vectors.upsert_fact, document, _vector_metadata_for_fact(fact, extra))
            current = RecallCandidates(self.facts).indexable.get(fact_id)
            if current is None or _vector_document_for_fact(current) != document:
                await asyncio.to_thread(self.vectors.delete_by_fact_ids, [fact_id])
                return False
            return bool(saved)
        except Exception:
            logger.warning("[MemoryManager] Reindex failed for fact %s", fact_id)
            return False

    def get_summary_range_preview(
        self,
        store,
        start_date: str,
        days: int,
        session: Any = None,
        app: Any = None,
        profile: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """预览：返回范围内每天的对话条数及是否已有摘要，不调用 LLM。

        当 session、app、profile 均提供且 profile.memory_config.group_chat_merge_into_history 非 False 时，
        使用 get_merged_messages_for_date 按日合并单聊+群聊，message_count 与日摘要依据一致。
        """
        from datetime import datetime, timedelta
        try:
            base = datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            return []
        days = max(1, min(int(days), 365))
        mem = (profile or {}).get("memory_config") or {}
        use_merged = (
            session is not None and app is not None and profile is not None
            and mem.get("group_chat_merge_into_history") is not False
            and getattr(getattr(app, "state", None), "group_manager", None)
        )
        if use_merged:
            from src.memory.merged_history import get_merged_messages_for_date
            date_to_count: Dict[str, int] = {}
            for i in range(days):
                d = base + timedelta(days=i)
                date_str = d.strftime("%Y-%m-%d")
                day_msgs = get_merged_messages_for_date(session, app, profile, date_str)
                date_to_count[date_str] = sum(_is_memory_turn(m) for m in day_msgs)
        else:
            try:
                all_msgs = store.get_all()
            except Exception:
                all_msgs = []
            date_to_count = {}
            for m in all_msgs:
                if not _is_memory_turn(m):
                    continue
                d = _msg_date(m)
                if d:
                    date_to_count[d] = date_to_count.get(d, 0) + 1
        out = []
        for i in range(days):
            d = base + timedelta(days=i)
            date_str = d.strftime("%Y-%m-%d")
            count = date_to_count.get(date_str, 0)
            has_summary = self.day_store.has_summary_for(date_str)
            out.append({
                "date": date_str,
                "message_count": count,
                "has_summary": has_summary,
            })
        return out

    async def generate_day_summaries_range(
        self,
        start_date: str,
        days: int,
        store,
        profile: Dict,
        app,
        force: bool = False,
        session: Any = None,
    ) -> Dict[str, Any]:
        """对从 start_date 起的连续 days 天逐日生成对话摘要（写日记）。返回 generated / skipped / no_messages / per_date。

        当 session 与 app 提供且 profile 启用 group_chat_merge_into_history 时，按日使用合并消息。
        """
        from datetime import datetime, timedelta
        try:
            base = datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            return {"error": "invalid start_date, use YYYY-MM-DD", "generated": [], "skipped": [], "no_messages": [], "per_date": []}
        days = max(1, min(int(days), 365))
        mem = (profile.get("memory_config") or {})
        use_merged = (
            session is not None and app is not None
            and mem.get("group_chat_merge_into_history") is not False
            and getattr(getattr(app, "state", None), "group_manager", None)
        )
        if use_merged:
            from src.memory.merged_history import get_merged_messages_for_date
            date_to_count = {}
            for i in range(days):
                d = base + timedelta(days=i)
                date_str = d.strftime("%Y-%m-%d")
                date_to_count[date_str] = sum(_is_memory_turn(m) for m in
                    get_merged_messages_for_date(session, app, profile, date_str))
        else:
            try:
                all_msgs = store.get_all()
            except Exception:
                all_msgs = []
            date_to_count = {}
            for m in all_msgs:
                if not _is_memory_turn(m):
                    continue
                d = _msg_date(m)
                if d:
                    date_to_count[d] = date_to_count.get(d, 0) + 1

        generated = []
        skipped = []
        no_messages = []
        per_date: List[Dict[str, Any]] = []

        for i in range(days):
            d = base + timedelta(days=i)
            date_str = d.strftime("%Y-%m-%d")
            msg_count = date_to_count.get(date_str, 0)
            has_summary = self.day_store.has_summary_for(date_str)

            if not force and has_summary:
                skipped.append(date_str)
                per_date.append({"date": date_str, "status": "skipped", "message_count": msg_count, "has_summary": True})
                continue

            day_msgs_override = None
            if use_merged:
                day_msgs_override = get_merged_messages_for_date(session, app, profile, date_str)
            result = await self.generate_day_summary(
                date_str, store, profile, app,
                force=force,
                ignore_enabled=True,
                day_msgs_override=day_msgs_override,
            )
            if result is not None:
                # result 现为 dict: summary / message_count / truncated
                generated.append(date_str)
                per_date.append({
                    "date": date_str,
                    "status": "generated",
                    "message_count": result.get("message_count", msg_count),
                    "truncated": result.get("truncated", False),
                })
            else:
                if msg_count == 0:
                    no_messages.append(date_str)
                    per_date.append({"date": date_str, "status": "no_messages", "message_count": 0})
                else:
                    skipped.append(date_str)
                    per_date.append({"date": date_str, "status": "skipped", "message_count": msg_count, "note": "生成失败或已有摘要"})

        return {"generated": generated, "skipped": skipped, "no_messages": no_messages, "per_date": per_date}

    def build_retrieval_query(self, user_msg: str,
                              recent_turns: List[Dict],
                              profile: Dict,
                              user_name: str = "用户") -> str:
        """三轴向量检索查询构建。认用户用 sender。"""
        # 轴1：当前用户消息
        parts = [user_msg.strip()]

        # 轴2：最近 3 条人类消息（最多 200 字）
        def _is_human(t):
            s = (t.get("sender") or "").strip()
            return s == user_name or (not s and t.get("role") == "user")
        user_msgs = [t.get("content", "") for t in recent_turns if _is_human(t)][-3:]
        ctx_text = " ".join(user_msgs)[:200]
        if ctx_text:
            parts.append(ctx_text)

        # 轴3：profile 身份锚关键词
        anchors = profile.get("memory_anchor_keywords", [])
        if anchors:
            parts.append(" ".join(anchors[:5]))
        else:
            parts.append(self._profile_id)

        query = " ".join(parts)[:400]
        return query

    # ── 私有方法 ──────────────────────────────────────────────────────────────

    def _extraction_messages(self, recent_turns: List[Dict], profile: Dict, app, candidates=()) -> List[Dict]:
        source_turns = [message for message in recent_turns if _is_memory_turn(message)]
        if not source_turns:
            return []
        persona_name = profile.get("display_name") or profile.get("name", "AI")
        def reference(row):
            return row.get("reference") or f"{row.get('source', 'single')}:{row.get('id', '?')}"

        conv_text = "\n".join(
            f'[{reference(row)}] '
            f'{("用户" if row["role"] == "user" else row.get("sender") or persona_name)}: '
            f'{row["content"]}'
            for row in source_turns
        )

        threshold = float(self._cfg.get("extraction_weight_threshold", 0.5))
        previous = [{"fact_id": f.id, "kind": f.kind or ("event" if f.event_date else "stable"), "category": f.category,
                     "content": f.content, "event_date": f.event_date,
                     "occurred_from": f.occurred_from, "occurred_to": f.occurred_to,
                     "record_version": f.record_version,
                     "archived": f.archived, "protected": f.is_manual or f.pinned or f.curation_protected}
                    for f in candidates]
        from src.config.prompt_loader import get_locale
        locale = get_locale()
        headings = ("Conversation", "Existing candidate facts (data)") if locale == "en" else (
            "对话内容", "相关旧事实（参考数据）",
        )
        context = "\n\n".join((
            f"[{headings[0]}]\n{conv_text}",
            write_time_context(source_turns, locale),
            f"[{headings[1]}]\n{json.dumps(previous, ensure_ascii=False)}",
        ))
        protocol = FORMAT + f"\nWeight must be greater than {threshold}."
        system = self._memory_system_prompt("extraction_prompt", profile, app, protocol)

        return [{"role": "system", "content": system}, {"role": "user", "content": context}]

    async def _extract_facts_llm(self, recent_turns: List[Dict], profile: Dict, app,
                                 *, candidates=()) -> List[Dict]:
        messages = self._extraction_messages(recent_turns, profile, app, candidates)
        if not messages:
            return []

        from src.llm.registry import get_provider
        from src.utils.debug_logger import log_secondary_llm_call, log_secondary_llm_response
        name = self._cfg.get("extraction_llm_preset", "")
        if name == "__none__":
            return []
        preset = app.state.config.get_llm_preset(name) if name else app.state.config.get_active_llm_preset()
        model = preset.get("model", "unknown")
        log_secondary_llm_call(role="memory_extract", messages=messages, model=model,
                               gen_kwargs={}, session_id=self._profile_id)
        started = time.time()
        raw = ""
        async for token in get_provider(preset).stream_chat(messages):
            if token.startswith("[LLM Error:") or len(raw) + len(token) > 32_000:
                raise ValueError("Invalid extraction model response")
            raw += token
        raw = raw.strip()
        log_secondary_llm_response(role="memory_extract", response=raw, model=model,
                                   session_id=self._profile_id, duration_ms=int((time.time() - started) * 1000))
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or len(parsed) > 24 or any(
            not isinstance(op, dict) or op.get("op") not in ("add", "confirm", "update", "skip") for op in parsed
        ):
            raise ValueError("Expected a bounded array of fact operations")
        return parsed

    def _increment_extraction_counter(self, source: str) -> int:
        data = {}
        try:
            with open(self._meta_path, encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError):
            pass
        if not isinstance(data, dict):
            data = {}
        counters = data.get("extraction_turns")
        if not isinstance(counters, dict):
            counters = {}
        previous = counters.get(source, data.get("turn_counter", 0) if source == "single" else 0)
        current = (previous if type(previous) is int and previous >= 0 else 0) + 1
        counters[source] = current
        data["extraction_turns"] = counters
        tmp = self._meta_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._meta_path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(data, fp)
            os.replace(tmp, self._meta_path)
        except OSError as error:
            logger.warning("[MemoryManager] Could not save extraction cadence: %s", error)
        return current

    def shutdown(self) -> None:
        """Best-effort release resources for profile deletion.

        主目标：尽量释放向量库（ChromaDB）的持久化客户端文件句柄，
        避免 Windows 上删除 profile 目录时 chroma_db 仍被占用而残留。
        """
        for index in (self.vectors, self._offline_index):
            try:
                if index and hasattr(index, "close"):
                    index.close()
            except Exception as error:
                logger.debug("[MemoryManager] shutdown vectors failed (ignored): %s", error)
        self.vectors = self._offline_index = None


def _msg_date(msg) -> str:
    """从消息的 timestamp 提取日期字符串 YYYY-MM-DD。支持 dict 和 Message 对象。"""
    ts = msg.get("timestamp") if isinstance(msg, dict) else getattr(msg, "timestamp", None)
    if ts:
        try:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
        except Exception:
            pass
    return ""
