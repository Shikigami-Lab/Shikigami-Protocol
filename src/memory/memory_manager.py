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
import os
import time
import traceback
from datetime import date, timedelta
from string import Template
from typing import Any, Dict, List, Optional, Tuple

from src.config.prompt_loader import get_prompt

from src.memory.long_term_store import LongTermStore
from src.memory.day_store import DaySummaryStore
from src.memory.vector_store import VectorMemoryStore
from src.memory.bm25_retriever import BM25Retriever
from src.utils.debug_logger import (
    log_secondary_llm_call,
    log_secondary_llm_response,
    log_error,
    log_memory_startup,
    log_fact_add,
    log_vector_add,
    log_vector_delete,
    log_memory_extract,
    log_summary_generate,
)
from src.utils.paths import get_project_root

logger = logging.getLogger(__name__)

_PROFILES_DIR = os.path.join(get_project_root(), "profiles")


# 前置过滤：时间相对词（此类事实时效性差，跳过）
_STALE_WORDS = frozenset(["刚才", "今天", "昨天", "刚刚", "这次", "这会儿", "现在", "今晚", "刚好"])


def _passes_filters(content: str) -> bool:
    """长度过滤 + 时间相对词过滤。"""
    if len(content.replace(" ", "")) < 12:
        return False
    for word in _STALE_WORDS:
        if word in content:
            return False
    return True


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
        "tags": list(fact.tags) if getattr(fact, "tags", None) else [],
    }
    if extra:
        meta.update(extra)
    return meta


def _extraction_format_block() -> str:
    """事实提取输出格式说明，从 config/system_prompts.yaml 读取。"""
    return get_prompt("memory.extraction_format")


def _consolidation_appendix(fact_list: str) -> str:
    """记忆合并任务说明，从 config/system_prompts.yaml 读取，替换 $fact_list。"""
    return Template(get_prompt("memory.consolidation")).safe_substitute(fact_list=fact_list)


# 向后兼容别名（内部代码仍可用，外部不导出）
def _get_extraction_format_block() -> str:
    return _extraction_format_block()


def _normalize_keywords(val: Any) -> List[str]:
    """将 LLM 返回的 keywords/关键词 规范为最多 2 个非空字符串的列表，供写入 tags。"""
    if val is None:
        return []
    if isinstance(val, list):
        out = [str(x).strip() for x in val if str(x).strip()][:2]
        return out
    s = str(val).strip()
    if not s:
        return []
    return [x for x in s.split() if x][:2]


class MemoryManager:

    def __init__(self, profile_id: str, storage_root: str,
                 profile_memory_cfg: Optional[Dict[str, Any]] = None,
                 app_memory_cfg: Optional[Dict[str, Any]] = None):
        self._profile_id = profile_id
        self._storage_root = storage_root

        # 合并配置：profile 覆盖全局，全局覆盖默认
        app_cfg = app_memory_cfg or {}
        prof_cfg = profile_memory_cfg or {}
        self._cfg: Dict[str, Any] = {
            "enabled": False,
            "vector_enabled": False,
            "extraction_frequency": 5,
            "extraction_weight_threshold": 0.5,
            "max_facts_in_prompt": 8,
            "extraction_prompt": "",
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
            "vector_dedup_distance_threshold": 0.1,  # 写入向量时：距离小于此值视为「几乎相同」删旧写新；0.1=仅近同才覆盖
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

        # 向量库（可选）：传入 embedding 配置 + 写入去重阈值（可从 memory 配置覆盖）
        self.vectors: Optional[VectorMemoryStore] = None
        if self._cfg.get("vector_enabled"):
            embed_cfg = {**self._cfg.get("embedding", {}), "vector_dedup_distance_threshold": float(self._cfg.get("vector_dedup_distance_threshold", 0.1))}
            self.vectors = VectorMemoryStore(storage_root, embed_cfg)

        # BM25 关键词检索（可选依赖 rank_bm25，缺失时静默降级）
        self.bm25 = BM25Retriever()

        # turn_counter（用于 auto_extract 频率控制）
        self._meta_path = os.path.join(storage_root, "memory_meta.json")
        self._turn_counter: int = self._load_turn_counter()

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

    def status(self) -> Dict[str, Any]:
        vec_ok = bool(self.vectors and self.vectors.is_available())
        vec_count = self.vectors.count() if vec_ok else 0
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
            "day_summary_enabled": bool(self._cfg.get("day_summary_enabled")),
        }

    def get_reflection_context_facts(
        self,
        max_count: int = 4,
        min_weight: float = 0.6,
    ) -> List[Dict[str, Any]]:
        """返回适合注入 Reflection 的高权重长期事实，供记忆引用话题。

        筛选：weight >= min_weight，优先 emotional_note 非空（情感相关），
        按 weight 降序再按 updated_at 降序。只返回 content/weight/updated_at 字段。
        """
        from datetime import datetime
        all_facts = self.facts.get_all()
        filtered = [f for f in all_facts if f.weight >= min_weight]
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
        for f in filtered[:max_count]:
            dt = datetime.fromtimestamp(f.updated_at).strftime("%Y-%m-%d")
            result.append({
                "content": f.content,
                "weight": round(f.weight, 2),
                "date": dt,
            })
        return result

    async def sync_facts_to_vectors(self) -> int:
        """将 facts store 中尚未入向量库的事实批量同步过去。返回新增条数。

        增量操作：已有 fact_id 对应向量的跳过，不覆盖。
        """
        if not self.vectors or not self.vectors.is_available():
            logger.warning("[MemoryManager] sync_facts_to_vectors: 向量库不可用")
            return 0
        existing = await asyncio.to_thread(self.vectors.get_all_fact_ids)
        count = 0
        for fact in self.facts.get_all():
            if fact.id in existing:
                continue
            meta = _vector_metadata_for_fact(fact)
            doc = _vector_document_for_fact(fact)
            await asyncio.to_thread(self.vectors.add, doc, metadata=meta)
            log_vector_add(self._profile_id, doc, fact_id=fact.id)
            count += 1
        logger.info("[MemoryManager] sync_facts_to_vectors 完成：新增 %d 条（共 %d 条事实）",
                    count, self.facts.count())
        return count

    async def auto_extract(self, recent_turns: List[Dict], profile: Dict, app,
                           emotion_context: Optional[Dict] = None) -> None:
        """LLM 自动提取事实（fire-and-forget 任务）。

        emotion_context: 可选，来自 EmotionEngine 的当前状态快照，
                         用于给向量记忆附加情感标签（18.1 情感记忆图谱）。
        """
        if not self._cfg.get("enabled"):
            return

        freq = int(self._cfg.get("extraction_frequency", 5))
        self._turn_counter += 1
        self._save_turn_counter()

        if self._turn_counter % freq != 0:
            return

        logger.info("[MemoryManager] auto_extract 触发 (turn=%d profile=%s)",
                    self._turn_counter, self._profile_id)

        try:
            facts_list = await self._extract_facts_llm(recent_turns, profile, app)
        except Exception as e:
            logger.error("[MemoryManager] _extract_facts_llm 失败: %s", e)
            log_error("memory", str(e), {"profile_id": self._profile_id}, traceback_str=traceback.format_exc())
            return

        # ── 批内去重：同次提取结果中的近似重复直接丢弃 ──────────────────────
        deduped: List[Dict] = []
        seen_tokens: List[set] = []
        for item in facts_list:
            toks = set(item.get("content", "").lower().split())
            if not toks:
                continue
            if any(len(toks & s) / len(toks | s) > 0.7 for s in seen_tokens if s):
                continue
            deduped.append(item)
            seen_tokens.append(toks)
        facts_list = deduped

        # 构建情感 metadata 片段（仅在 emotion_context 有值时附加）
        emotion_meta: Dict[str, Any] = {}
        if emotion_context:
            emotion_meta["ai_emotion"]   = emotion_context.get("primary_emotion", "")
            emotion_meta["ai_energy"]    = float(emotion_context.get("energy_level", 80.0))
            # user_sentiment 预留字段（待 NLI 分类器接入后填充）
            if emotion_context.get("user_sentiment"):
                emotion_meta["user_sentiment"] = emotion_context["user_sentiment"]

        threshold = float(self._cfg.get("extraction_weight_threshold", 0.5))
        added_count = 0
        for item in facts_list:
            content = (item.get("content", "") or "").strip()[:50]  # 单条事实不超过50字
            category = item.get("category", "other")
            emotional_note = item.get("emotional_note", "")
            # weight 0-2，与 score 公式兼容
            raw_weight = item.get("weight")
            if raw_weight is None:
                raw_weight = item.get("confidence")  # 兼容旧格式：若仍有 confidence 可按 (x/5)*2 转为 0-2
            try:
                w = float(raw_weight) if raw_weight is not None else 1.0
                if 0 <= w <= 2:
                    weight = round(w, 2)
                else:
                    # 兼容旧 1-5 的 confidence：映射到 0-2
                    if 1 <= w <= 5:
                        weight = round((w - 1) / 4 * 2, 2)  # 1->0, 5->2
                    else:
                        weight = round(max(0.0, min(2.0, w)), 2)
            except (TypeError, ValueError):
                weight = 1.0

            tags = item.get("keywords", [])
            if not isinstance(tags, list):
                tags = _normalize_keywords(tags)

            if not _passes_filters(content):
                continue
            # weight 阈值：小于等于阈值的不采纳
            if weight <= threshold:
                logger.debug("[MemoryManager] 低权重事实跳过 (weight=%.2f <= %.2f): %.40s",
                             weight, threshold, content)
                continue

            fact = self.facts.add(content, category=category, source="auto",
                                  emotional_note=emotional_note, tags=tags, weight=weight)
            if fact:
                added_count += 1
                log_fact_add(
                    self._profile_id,
                    fact.id,
                    fact.content,
                    fact.category,
                    fact.source,
                    weight=fact.weight,
                    tags=fact.tags,
                    pinned=fact.pinned,
                    emotional_note=fact.emotional_note,
                    updated_at=fact.updated_at,
                    is_manual=fact.is_manual,
                )
                # 同步写向量库：嵌入文本 = 事实正文 + tags，参与语义阈值；metadata 不含 weight/pinned
                if self.vectors and self.vectors.is_available():
                    vec_meta = _vector_metadata_for_fact(fact, extra=emotion_meta)
                    doc = _vector_document_for_fact(fact)
                    await asyncio.to_thread(self.vectors.add, doc, metadata=vec_meta)
                    log_vector_add(self._profile_id, doc, fact_id=fact.id)

        log_memory_extract(self._profile_id, self._turn_counter,
                           len(facts_list), added_count)
        logger.info("[MemoryManager] auto_extract 完成 profile=%s extracted=%d added=%d",
                    self._profile_id, len(facts_list), added_count)
        # 人格演化触发已移出此处：旧实现挂在 auto_extract 末尾，会被 memory 总开关
        # 绑死，且 turn_counter % interval 的判定在频率不整除时会永远命不中。
        # 现由 chat.py::_fire_persona_evolution_check 按消息计数独立触发。

    def _build_day_summary_context(self, date_str: str, msg_count: int, profile: Dict, app) -> Dict[str, str]:
        """收集日摘要所需的上下文变量：角色名、近期日记、好感状态、活跃度备注。"""
        # 角色名
        character_name = (profile or {}).get("display_name", "") or self._profile_id

        # 近期日记（最多 3 条，放在对话内容之前）
        recent = self.day_store.get_recent(4)  # 取 4 条，排除当天后留 3 条
        recent = [s for s in recent if s.get("date") != date_str][-3:]
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
            history = self.day_store.get_recent(7)
            active_days = [s for s in history if s.get("message_count", 0) > 0 and s.get("date") != date_str]
            if len(active_days) >= 2:
                avg = sum(s["message_count"] for s in active_days) / len(active_days)
                if avg > 0:
                    ratio = msg_count / avg
                    if ratio >= 1.6:
                        activity_note = f"（今天聊了 {msg_count} 条，比平时多不少，平均 {avg:.0f} 条）"
                    elif ratio <= 0.4:
                        activity_note = f"（今天只聊了 {msg_count} 条，比平时少很多，平均 {avg:.0f} 条）"

        return {
            "character_name": character_name,
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
        if not ignore_enabled and not self._cfg.get("day_summary_enabled"):
            return None
        if not force and self.day_store.has_summary_for(date_str):
            return None

        logger.info("[MemoryManager] 生成日摘要 date=%s profile=%s quiet=%s", date_str, self._profile_id, quiet)

        def _msg_role(m): return m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        def _msg_content(m): return m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        def _msg_sender(m): return (m.get("sender") if isinstance(m, dict) else getattr(m, "sender", None)) or ""

        user_name = getattr(getattr(app, "state", None) and getattr(app.state, "config", None), "user_name", "用户") or "用户"
        def _is_human(m): return _msg_sender(m).strip() == user_name or (not _msg_sender(m).strip() and _msg_role(m) == "user")

        if quiet:
            day_msgs = []
        elif day_msgs_override is not None:
            day_msgs = [m for m in day_msgs_override
                        if _msg_date(m) == date_str and _msg_role(m) in ("user", "assistant")]
        else:
            try:
                all_msgs = store.get_all()
            except Exception as e:
                logger.error("[MemoryManager] 读取对话历史失败: %s", e)
                log_error("memory", str(e), {"profile_id": self._profile_id, "op": "read_history"}, traceback_str=traceback.format_exc())
                return None
            day_msgs = [m for m in all_msgs
                        if _msg_date(m) == date_str and _msg_role(m) in ("user", "assistant")]

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
                f"{'用户' if _is_human(m) else 'AI'}: {_msg_content(m)}"
                for m in day_msgs
            )
            max_conv_chars = max(2000, min(100000, int(self._cfg.get("day_summary_max_conv_chars", 12000))))
            if len(conv_text) > max_conv_chars:
                conv_text = conv_text[:max_conv_chars] + "\n...（内容过长，已截断）"
                truncated_text = True

        ctx = self._build_day_summary_context(date_str, len(day_msgs), profile, app)

        custom_summary_prompt = self._cfg.get("day_summary_prompt", "")
        if custom_summary_prompt:
            prompt_template = custom_summary_prompt.replace("{conversation}", conv_text).replace("{date}", date_str)
        elif quiet:
            prompt_template = Template(get_prompt("memory.day_summary_quiet")).safe_substitute(
                date_str=date_str,
                character_name=ctx["character_name"],
                recent_summaries=ctx["recent_summaries"],
                affinity_status=ctx["affinity_status"],
            )
        else:
            prompt_template = Template(get_prompt("memory.day_summary")).safe_substitute(
                date_str=date_str,
                conv_text=conv_text,
                character_name=ctx["character_name"],
                recent_summaries=ctx["recent_summaries"],
                affinity_status=ctx["affinity_status"],
                activity_note=ctx["activity_note"],
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
            # 至少需要一条 user 消息，否则 Gemini 等 API 会报 contents is not specified
            _summary_msgs = [
                {"role": "system", "content": prompt_template},
                {"role": "user", "content": get_prompt("memory.day_summary_user")},
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
                                           keep_days=keep_days)
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

    async def run_consolidation_batch(
        self, app: Any, profile: Dict[str, Any], batch: List[Any]
    ) -> Tuple[bool, Dict[str, Any]]:
        """将一批「已很淡」的事实交给 LLM 合并为 1～2 条新事实，新摘要入库 + 移除旧事实向量（事实库不删）。
        返回 (成功, detail)；detail 含 added/added_contents/vectors_removed/vectors_removed_contents/count，
        供记忆变更日志记录，失败或无事可做时为 {}。"""
        if not batch:
            return True, {}
        valid_categories = {"habit", "preference", "taboo", "relationship", "location", "milestone", "ai_insight", "other"}
        fact_list = "\n".join(
            f"{f.content} [{getattr(f, 'category', 'other')}] [{getattr(f, 'emotional_note', '')}]"
            for f in batch
        )
        base_prompt = (profile.get("memory_config") or {}).get("extraction_prompt") or self._cfg.get("extraction_prompt") or "从对话中提取事实，保持角色口吻。"
        system_content = base_prompt + _consolidation_appendix(fact_list) + _extraction_format_block()
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": get_prompt("memory.consolidation_user")},
        ]
        raw = ""
        try:
            from src.llm.registry import get_provider
            extraction_preset_name = self._cfg.get("extraction_llm_preset", "")
            # 特殊值："__none__"=不做合并；""=使用激活模型
            if extraction_preset_name == "__none__":
                return False, {}
            if extraction_preset_name:
                preset = app.state.config.get_llm_preset(extraction_preset_name)
            else:
                preset = app.state.config.get_active_llm_preset()
            _model = (preset or {}).get("model", "unknown")
            log_secondary_llm_call(
                role="memory_consolidation",
                messages=messages,
                model=_model,
                gen_kwargs={},
                session_id=self._profile_id,
            )
            llm = get_provider(preset)
            t0 = time.time()
            async for token in llm.stream_chat(messages):
                raw += token
            log_secondary_llm_response(
                role="memory_consolidation",
                response=raw,
                model=_model,
                session_id=self._profile_id,
                duration_ms=int((time.time() - t0) * 1000),
            )
        except Exception as e:
            logger.warning("[MemoryManager] 合并 LLM 调用失败 profile=%s: %s", self._profile_id, e)
            return False, {}
        raw = raw.strip()
        for prefix in ("```json", "```"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[MemoryManager] 合并 JSON 解析失败 profile=%s，重试一次", self._profile_id)
            try:
                from src.llm.registry import get_provider as _get_provider
                _preset = app.state.config.get_llm_preset(extraction_preset_name) if extraction_preset_name else app.state.config.get_active_llm_preset()
                _llm = _get_provider(_preset)
                messages_retry = messages + [{"role": "user", "content": "请严格只输出 JSON 数组，不要任何解释。"}]
                raw = ""
                async for token in _llm.stream_chat(messages_retry):
                    raw += token
                raw = raw.strip()
                for p in ("```json", "```"):
                    if raw.startswith(p):
                        raw = raw[len(p):].strip()
                if raw.endswith("```"):
                    raw = raw[:-3].strip()
                data = json.loads(raw)
            except Exception as e2:
                logger.warning("[MemoryManager] 合并重试仍失败 profile=%s: %s", self._profile_id, e2)
                return False, {}
        if not isinstance(data, list):
            return False, {}
        min_old_updated = min(f.updated_at for f in batch)
        valid_items = []
        # 合并产出是 1～2 条概括性陈述，允许比「单条提取 50 字」更长，上限 500 字
        CONSOLIDATION_CONTENT_MAX = 500
        for item in data[:2]:
            content = (item.get("content") or "").strip()
            if not content or len(content) > CONSOLIDATION_CONTENT_MAX:
                continue
            cat = item.get("category", "other")
            if cat not in valid_categories:
                cat = "other"
            try:
                w = float(item.get("weight", 1.0))
                weight = round(max(0.0, min(2.0, w)), 2)
            except (TypeError, ValueError):
                weight = 1.0
            emotional_note = (item.get("emotional_note") or "")[:15]
            tags = item.get("keywords", [])
            if not isinstance(tags, list):
                tags = _normalize_keywords(tags)
            valid_items.append({"content": content, "category": cat, "weight": weight, "emotional_note": emotional_note, "tags": tags})
        if not valid_items:
            return False, {}
        added = []
        for it in valid_items:
            fact = self.facts.add(
                it["content"],
                category=it["category"],
                weight=it["weight"],
                source="auto",
                emotional_note=it["emotional_note"],
                tags=it["tags"],
                updated_at=min_old_updated,
            )
            if not fact:
                logger.warning("[MemoryManager] 合并写新事实失败（重复或错误）profile=%s", self._profile_id)
                return False, {}
            added.append(fact)
            log_fact_add(
                self._profile_id,
                fact.id,
                fact.content,
                fact.category,
                "consolidation",
                weight=fact.weight,
                tags=fact.tags,
                pinned=fact.pinned,
                emotional_note=fact.emotional_note or "",
                updated_at=fact.updated_at,
                is_manual=fact.is_manual,
            )
            if self.vectors and self.vectors.is_available():
                doc = _vector_document_for_fact(fact)
                meta = _vector_metadata_for_fact(fact)
                await asyncio.to_thread(self.vectors.add, doc, metadata=meta, skip_dedup=True)
                log_vector_add(self._profile_id, doc, fact_id=fact.id)
        # 事实库为永久 archive：合并只新增摘要事实，不删除被合并的旧事实（仅手动删除可删事实）。
        # 仅从向量库移除旧事实的向量，避免检索时同时命中旧条与摘要造成重复。
        old_ids = [f.id for f in batch]
        if self.vectors and self.vectors.is_available():
            n_vec = self.vectors.delete_by_fact_ids(old_ids)
            log_vector_delete(self._profile_id, n_vec, "consolidation", fact_ids=old_ids)
        logger.info(
            "[MemoryManager] 合并完成 profile=%s 旧向量删除 %d 新事实写入 %d（事实库未删，永久保留）",
            self._profile_id, len(old_ids), len(added),
        )
        # detail 供 memory_changelog 记录：added=新摘要 fact id；vectors_removed=被移除向量的旧事实 id；
        # *_contents 为对应全文（日志展示用，不截断）
        detail = {
            "added": [f.id for f in added],
            "added_contents": [f.content for f in added],
            "vectors_removed": list(old_ids),
            "vectors_removed_contents": [f.content for f in batch],
            "count": len(added),
        }
        return True, detail

    async def reembed_fact(self, fact_id: str) -> bool:
        """把某条仍在档案里的事实重新嵌入向量库（记忆变更回滚用）。"""
        if not (self.vectors and self.vectors.is_available()):
            return False
        fact = self.facts.get_by_id(fact_id)
        if not fact:
            return False
        try:
            doc = _vector_document_for_fact(fact)
            meta = _vector_metadata_for_fact(fact)
            await asyncio.to_thread(self.vectors.add, doc, metadata=meta, skip_dedup=True)
            return True
        except Exception as e:
            logger.warning("[MemoryManager] reembed_fact 失败 %s: %s", fact_id, e)
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
                date_to_count[date_str] = len(day_msgs)
        else:
            try:
                all_msgs = store.get_all()
            except Exception:
                all_msgs = []
            def _role(m): return m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
            date_to_count = {}
            for m in all_msgs:
                if _role(m) not in ("user", "assistant"):
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
                date_to_count[date_str] = len(get_merged_messages_for_date(session, app, profile, date_str))
        else:
            try:
                all_msgs = store.get_all()
            except Exception:
                all_msgs = []
            def _role(m): return m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
            date_to_count = {}
            for m in all_msgs:
                if _role(m) not in ("user", "assistant"):
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

    async def _extract_facts_llm(self, recent_turns: List[Dict],
                                 profile: Dict, app) -> List[Dict]:
        """LLM 提取事实列表，返回 [{content, category}]。认用户用 sender。"""
        if not recent_turns:
            return []

        user_name = getattr(getattr(app, "state", None) and getattr(app.state, "config", None), "user_name", "用户") or "用户"
        persona_name = profile.get("display_name") or profile.get("name", "AI")
        user_label = "用户"

        def _is_human(m):
            s = (m.get("sender") or "").strip()
            return s == user_name or (not s and m.get("role") == "user")

        conv_text = "\n".join(
            f"{user_label if _is_human(m) else persona_name}: {m.get('content', '')}"
            for m in recent_turns
            if m.get("role") in ("user", "assistant")
        )
        if not conv_text.strip():
            logger.warning(
                "[MemoryManager] recent_turns 中无 user/assistant 消息，跳过提取 (profile=%s len=%s)",
                self._profile_id, len(recent_turns),
            )
            return []

        custom_prompt = self._cfg.get("extraction_prompt", "")
        if custom_prompt:
            extraction_prompt = custom_prompt.replace("{conversation}", conv_text)
            # 若自定义模板里没有占位符 {conversation}，replace 不会插入对话，导致模型只收到人设而输出角色话而非 JSON
            if conv_text.strip() and conv_text not in extraction_prompt:
                logger.warning(
                    "[MemoryManager] extraction_prompt 中未包含 {conversation}，已强制追加对话 (profile=%s)",
                    self._profile_id,
                )
                extraction_prompt = extraction_prompt.rstrip() + "\n\n对话内容：\n" + conv_text
            # 无论自定义内容如何，末尾统一追加格式块与采纳阈值说明
            threshold = float(self._cfg.get("extraction_weight_threshold", 0.5))
            extraction_prompt = extraction_prompt.rstrip() + _extraction_format_block()
            extraction_prompt += f"\n当前采纳阈值：weight 需大于 {threshold} 才会被采纳（即 weight 小于等于 {threshold} 的事实不会被采纳）。\n"
        else:
            threshold = float(self._cfg.get("extraction_weight_threshold", 0.5))
            extraction_prompt = (
                Template(get_prompt("memory.extraction_default")).safe_substitute(
                    user_label=user_label,
                    persona_name=persona_name,
                    conv_text=conv_text,
                )
                + _extraction_format_block()
                + f"\n当前采纳阈值：weight 需大于 {threshold} 才会被采纳（即 weight 小于等于 {threshold} 的事实不会被采纳）。\n"
            )

        try:
            from src.llm.registry import get_provider
            from src.utils.debug_logger import log_secondary_llm_call, log_secondary_llm_response
            # 支持独立的提取模型配置；未配置则用当前激活 preset
            extraction_preset_name = self._cfg.get("extraction_llm_preset", "")
            # 特殊值："__none__"=不做记忆提取；""=使用激活模型
            if extraction_preset_name == "__none__":
                logger.debug("[MemoryManager] extraction_llm_preset='__none__', skip extraction")
                return []
            if extraction_preset_name:
                preset = app.state.config.get_llm_preset(extraction_preset_name)
            else:
                preset = app.state.config.get_active_llm_preset()
            llm = get_provider(preset)
            _extract_model = preset.get("model", "unknown") if preset else "unknown"
            _extract_msgs = [{"role": "user", "content": extraction_prompt}]
            log_secondary_llm_call(
                role="memory_extract",
                messages=_extract_msgs,
                model=_extract_model,
                gen_kwargs={},
                session_id=self._profile_id,
            )
            _t0 = time.time()
            raw = ""
            _llm_exc = None
            for _attempt in range(2):  # 最多重试 1 次
                try:
                    raw = ""
                    async for token in llm.stream_chat(_extract_msgs):
                        raw += token
                    _llm_exc = None
                    break
                except Exception as _e:
                    _llm_exc = _e
                    if _attempt == 0:
                        logger.warning("[MemoryManager] _extract_facts_llm stream 失败，重试: %s", _e)
                        import asyncio as _asyncio
                        await _asyncio.sleep(3.0)
            if _llm_exc:
                raise _llm_exc

            log_secondary_llm_response(
                role="memory_extract",
                response=raw,
                model=_extract_model,
                session_id=self._profile_id,
                duration_ms=int((time.time() - _t0) * 1000),
            )

            # 解析 JSON，兼容两种格式：
            #   新格式：[{"content": "...", "category": "..."}]
            #   旧格式：{"facts": ["字符串1", "字符串2"]}
            raw = raw.strip()
            if raw.startswith("```"):
                lines = raw.splitlines()
                raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            parsed = json.loads(raw)

            # 统一为 [{"content": ..., "category": ...}]
            if isinstance(parsed, dict) and "facts" in parsed:
                parsed = parsed["facts"]
            if not isinstance(parsed, list):
                logger.warning("[MemoryManager] 事实提取结果非数组结构，跳过: %.60s", str(parsed)[:60])
                return []

            _VALID_CATEGORIES = frozenset([
                "habit", "preference", "taboo", "relationship",
                "location", "milestone", "ai_insight", "other",
            ])
            result = []
            for item in parsed:
                if isinstance(item, str):
                    result.append({
                        "content": item, "category": "other",
                        "emotional_note": "", "weight": 1.0, "keywords": [],
                    })
                elif isinstance(item, dict) and item.get("content"):
                    category = item.get("category", "other")
                    if category not in _VALID_CATEGORIES:
                        logger.debug("[MemoryManager] 未知 category '%s'，归入 other", category)
                        category = "other"
                    # weight 0-2；兼容旧字段 confidence(1-5) 转为 0-2
                    raw_w = item.get("weight")
                    if raw_w is not None:
                        try:
                            w = max(0.0, min(2.0, float(raw_w)))
                        except (TypeError, ValueError):
                            w = 1.0
                    else:
                        c = item.get("confidence")
                        if c is not None:
                            try:
                                c = max(0, min(5, int(c)))
                                w = (c - 1) / 4 * 2 if c >= 1 else 0.0  # 1->0, 5->2
                            except (TypeError, ValueError):
                                w = 1.0
                        else:
                            w = 1.0
                    result.append({
                        "content": item.get("content", "").strip(),
                        "category": category,
                        "emotional_note": str(item.get("emotional_note") or "").strip()[:15],
                        "weight": w,
                        "keywords": _normalize_keywords(item.get("keywords") or item.get("关键词")),
                    })
            return result
        except json.JSONDecodeError as e:
            logger.warning("[MemoryManager] 事实提取 JSON 解析失败: %s | raw=%.100s", e, raw)
        except Exception as e:
            logger.error("[MemoryManager] _extract_facts_llm 失败: %s", e)
            log_error("memory", str(e), {"profile_id": self._profile_id, "op": "extract_llm"}, traceback_str=traceback.format_exc())
        return []

    def _load_turn_counter(self) -> int:
        try:
            if os.path.exists(self._meta_path):
                with open(self._meta_path, "r", encoding="utf-8") as f:
                    return int(json.load(f).get("turn_counter", 0))
        except Exception:
            pass
        return 0

    def _save_turn_counter(self):
        try:
            os.makedirs(os.path.dirname(self._meta_path), exist_ok=True)
            tmp = self._meta_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"turn_counter": self._turn_counter}, f)
            os.replace(tmp, self._meta_path)
        except Exception as e:
            logger.warning("[MemoryManager] 保存 turn_counter 失败: %s", e)

    def shutdown(self) -> None:
        """Best-effort release resources for profile deletion.

        主目标：尽量释放向量库（ChromaDB）的持久化客户端文件句柄，
        避免 Windows 上删除 profile 目录时 chroma_db 仍被占用而残留。
        """
        try:
            if self.vectors and hasattr(self.vectors, "close"):
                self.vectors.close()
        except Exception as e:
            logger.debug("[MemoryManager] shutdown vectors failed (ignored): %s", e)

        # Help GC and prevent further vector access.
        self.vectors = None


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
