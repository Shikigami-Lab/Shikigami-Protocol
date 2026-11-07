"""长期事实记忆 Segment — priority=82

检索策略（3 槽位）：
  槽1 Pinned  — 全部置顶事实，不占用非钉选配额
  槽2 近期    — updated_at < recent_days 天内，按 score 取前 recent_fact_quota 条
  槽3 混合    — BM25 关键词 + 向量语义 双路检索，RRF 融合，最多 semantic_fact_quota 条且不超过 (max_facts_in_prompt − 槽2已用)
非钉选总量不超过 max_facts_in_prompt。

情感注记：与当前情绪匹配或带 emotional_note 的 fact 在槽内优先；并在 block 前加一句显式强调。
"""
import json
import logging
import os
from collections import defaultdict
from typing import Any, Dict, List

from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register
from src.utils.paths import get_project_root
from src.memory.fact_operations import normalize, rank_related
from src.memory.recall import RecallCandidates, search_vectors
from src.utils.memory_time import read_time_notice, source_time

logger = logging.getLogger(__name__)

# 当前情绪（英文）-> 可能出现在 emotional_note 中的中文关键词，用于检索时加权
_EMOTION_NOTE_KEYWORDS: Dict[str, List[str]] = {
    "sad": ["心疼", "难过", "伤心", "遗憾", "不舍"],
    "joyful": ["开心", "高兴", "欣慰", "温暖", "欢喜"],
    "anxious": ["担心", "不安", "牵挂", "焦虑"],
    "tender": ["心疼", "温暖", "感动", "珍惜", "温柔"],
    "warm": ["温暖", "开心", "欣慰", "感动"],
    "affectionate": ["珍惜", "温暖", "感动", "在意"],
    "tired": ["心疼", "担心", "无奈"],
    "frustrated": ["无奈", "担心", "遗憾"],
    "melancholic": ["难过", "心疼", "遗憾", "不舍"],
    "worried": ["担心", "不安", "牵挂"],
    "excited": ["开心", "期待", "高兴"],
    "inspired": ["欣慰", "感动", "开心"],
    "playful": ["开心", "欢喜", "温暖"],
    "playful_teasing": ["开心", "欢喜"],
    "confident": ["欣慰", "开心", "信任"],
    "curious": ["在意", "关心"],
    "surprised": ["意外", "惊喜", "在意"],
}

# 有语义的分类才显示标题，other 归入无标题组
_CATEGORY_LABELS = {
    "taboo":        "[禁忌与雷区]",
    "relationship": "[关系与羁绊]",
    "habit":        "[习惯与偏好]",
    "preference":   "[习惯与偏好]",
    "milestone":    "[重要时刻]",
    "location":     "[地点与空间]",
    "ai_insight":   "[式神的洞察]",
}

# 分类显示顺序
_CATEGORY_ORDER = ["taboo", "relationship", "habit", "preference", "milestone", "location", "ai_insight", "other"]


def _get_current_emotion(profile_id: str) -> str:
    """从 emotion_state.json 读取当前主情绪（英文）。"""
    path = os.path.join(get_project_root(), "profiles", profile_id, "emotion_state.json")
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        return (state.get("primary_emotion") or "").strip()
    except Exception:
        return ""


def _emotional_note_matches_current(fact: Any, current_emotion: str) -> bool:
    """当前情绪与事实的 emotional_note 是否相近（用于检索加权）。"""
    if not current_emotion:
        return False
    note = (getattr(fact, "emotional_note", None) or "").strip()
    if not note:
        return False
    keywords = _EMOTION_NOTE_KEYWORDS.get(current_emotion, [])
    return any(kw in note for kw in keywords)


def _rrf_merge(bm25_ranked, vec_ranked, k=60):
    """Reciprocal Rank Fusion：合并 BM25 和向量两路检索结果。

    Args:
        bm25_ranked: BM25 结果 ID，已按 score 降序
        vec_ranked: 向量结果 ID，已按 distance 升序（dist 越小越相关）
        k: RRF 平滑参数（默认 60，数值越大对头部排名越不敏感）

    Returns:
        List[fact_id]，按 RRF 融合分数降序排列。
        同时出现在两路结果中的 fact 得到双重加分（混合命中奖励）。
    """
    scores = {}
    for rank, fid in enumerate(bm25_ranked):
        scores[fid] = scores.get(fid, 0.0) + 1.0 / (k + rank + 1)
    for rank, fid in enumerate(vec_ranked):
        scores[fid] = scores.get(fid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda fid: scores[fid], reverse=True)


@register
class LongTermFactsSegment(PromptSegment):
    segment_id = "long_term_facts"
    priority = 82
    label = "长期事实记忆"
    description = "注入 AI 记住的关于用户的长期持久性事实"
    is_core = False
    default_trigger_mode = "always"

    def build(self, ctx: BuildContext) -> SegmentResult:
        app = ctx.extras.get("app")
        if not app:
            return SegmentResult(fired=False)

        mgr = _get_manager(app, ctx)
        if not mgr or not mgr._cfg.get("enabled"):
            return SegmentResult(fired=False)

        override = ctx.extras.get("max_facts_override")
        max_k = int(override) if override is not None else int(mgr._cfg.get("max_facts_in_prompt", 8))
        recent_days = int(mgr._cfg.get("recent_days", 3))
        recent_quota = int(mgr._cfg.get("recent_fact_quota", 3))
        semantic_quota = int(mgr._cfg.get("semantic_fact_quota", 2))  # 槽3 最多条数（与「语义槽位」一致）
        semantic_dist = float(mgr._cfg.get("semantic_distance", 0.45))

        recent_cutoff = recent_days * 86400
        current_address = ctx.extras.get("current_address_fact_id")
        query = ctx.user_msg.strip()

        def select_slots(pool):
            active = [fact for fact in pool.active.values() if fact.id != current_address]
            pinned = [fact for fact in active if fact.pinned]
            recent = rank_related(
                [fact for fact in active if not fact.pinned and pool.now - fact.updated_at < recent_cutoff],
                query, min(recent_quota, max_k),
            )
            excluded = {current_address, *(fact.id for fact in pinned + recent)}
            cap = min(max(0, max_k - len(recent)), semantic_quota)
            return pinned, recent, cap, pool.ranked_sources(excluded) if cap > 0 and query else {}

        pool = RecallCandidates(mgr.facts)
        pinned, recent, slot3_cap, sources = select_slots(pool)
        vector_rows = []
        if sources and mgr.vectors and mgr.vectors.is_available():
            vector_rows = search_vectors(mgr.vectors, query, min(slot3_cap * 4, 60))
            # The embedding call can outlast a concurrent edit, expiry, or archive.
            pool = RecallCandidates(mgr.facts)
            pinned, recent, slot3_cap, sources = select_slots(pool)

        semantic = []
        if sources:
            candidates = list(sources.values())
            related = rank_related(candidates, query, len(candidates))
            lexical_ids = {fact.id for fact in related}
            bm25_ranked = []
            bm25 = getattr(mgr, "bm25", None)
            if bm25 and bm25.is_available():
                try:
                    bm25_ranked = list(dict.fromkeys(
                        pool.resolve(fid).id for fid, _ in bm25.search(candidates, query, limit=slot3_cap * 4)
                        if fid in lexical_ids
                    ))
                except Exception as e:
                    logger.debug("[LongTermFactsSegment] BM25 search 失败: %s", e)
            vec_ranked = []
            if vector_rows:
                vec_ranked = [fact.id for fact, _ in pool.vector_hits(
                    sources, vector_rows, mgr.fact_vector_matches, semantic_dist,
                )]
            ranked = _rrf_merge(bm25_ranked, vec_ranked)
            if not ranked:
                ranked = list(dict.fromkeys(pool.resolve(fact.id).id for fact in related))
            semantic = [pool.resolve(fid) for fid in ranked[:slot3_cap]]

        # 槽内二次排序：与当前情绪匹配的 emotional_note 最优先，其次有 emotional_note 的优先
        current_emotion = _get_current_emotion(ctx.session.profile_id)
        def _emotion_sort_key(f):
            match = _emotional_note_matches_current(f, current_emotion)
            has_note = bool(getattr(f, "emotional_note", ""))
            return (0 if match else 1, 0 if has_note else 1)
        recent.sort(key=_emotion_sort_key)
        semantic.sort(key=_emotion_sort_key)
        top_facts = []
        seen = set()
        unpinned_experience_shown = False
        for fact in pinned + recent + semantic:
            if fact.kind == "experience" and not fact.pinned:
                if unpinned_experience_shown:
                    continue
                unpinned_experience_shown = True
            key = (normalize(fact.content), fact.event_date, fact.occurred_from, fact.occurred_to)
            if key not in seen:
                seen.add(key)
                top_facts.append(fact)

        if not top_facts:
            return SegmentResult(fired=False)

        # ── 格式化（按分类分组） ─────────────────────────────────────────────
        groups = defaultdict(list)
        experiences = []
        for fact in top_facts:
            if fact.kind == "experience":
                experiences.append(fact)
            else:
                groups[fact.category].append(fact)
        if experiences:
            ctx.extras["recalled_experiences"] = experiences

        lines = []
        idx = 1
        seen_labels = set()

        for cat in _CATEGORY_ORDER:
            if cat not in groups:
                continue
            label = _CATEGORY_LABELS.get(cat, "")
            if label and label not in seen_labels:
                lines.append(label)
                seen_labels.add(label)
            for f in groups[cat]:
                note = getattr(f, "emotional_note", "")
                time_str = ('事件日期 ' + f.event_date) if f.event_date else ('记录更新于 ' + source_time(f.updated_at))
                suffix = f"（{note}，{time_str}）" if note else f"（{time_str}）"
                lines.append(f"{idx}. {f.content}{suffix}")
                idx += 1

        # 兜底：处理未在 _CATEGORY_ORDER 中的分类
        for cat, facts in groups.items():
            if cat in _CATEGORY_ORDER:
                continue
            for f in facts:
                note = getattr(f, "emotional_note", "")
                time_str = ('事件日期 ' + f.event_date) if f.event_date else ('记录更新于 ' + source_time(f.updated_at))
                suffix = f"（{note}，{time_str}）" if note else f"（{time_str}）"
                lines.append(f"{idx}. {f.content}{suffix}")
                idx += 1

        if experiences:
            lines.append("【值得记住的经历】")
            for fact in experiences:
                when = fact.occurred_from or fact.occurred_to or "发生时间未确认"
                if fact.occurred_from and fact.occurred_to and fact.occurred_from != fact.occurred_to:
                    when = f"{fact.occurred_from} 至 {fact.occurred_to}"
                lines.append(f"{idx}. {fact.content}（{when}）")
                idx += 1

        has_emotional = any(getattr(f, "emotional_note", "") for f in top_facts)
        header = (
            "【长期认知与记忆】以下记忆中带「情感注记」的条目是角色特别在意的，回复时可酌情呼应。\n\n以下是与你当前最相关的记忆："
            if has_emotional
            else "【长期认知与记忆】以下是与你当前最相关的记忆："
        )
        content = header + "\n" + read_time_notice() + "\n" + "\n".join(lines)
        return SegmentResult(messages=[{"role": "system", "content": content}])


def _get_manager(app, ctx: BuildContext):
    managers = getattr(app.state, "memory_managers", {})
    return managers.get(ctx.session.profile_id)
