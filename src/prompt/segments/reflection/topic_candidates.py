"""reflection_topic_candidates —— 主动话题候选注入（自省）。

替换旧的 reflection/trend_context.py。遍历所有启用的 TopicSource 收集候选，
跨来源轮转取样保证「平等」，注入自省 prompt 供 LLM 用 topic_pick 选定一个。
候选连同复合 ID 一并暴露在 ctx.topic_candidates，供 _reflect() 解析 topic_pick。

inject_into="reflection"，priority=56（persona 类段落之后）。
"""
import logging
import os

from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_locale
from src.utils.paths import get_project_root

logger = logging.getLogger(__name__)


def _recently_used_refs(storage_root: str, window: int) -> set:
    """从 ase_state.json 的 proactive_log 取最近 window 条已发话题的复合 ID。"""
    from src.core.ase import _load_ase_state
    refs: set = set()
    for entry in (_load_ase_state(storage_root).get("proactive_log") or [])[-window:]:
        s = entry.get("source")
        i = entry.get("item_id")
        if s and i:
            refs.add(f"{s}:{i}")
    return refs


def _recent_spoken_sources(storage_root: str, n: int) -> list:
    """最近 n 次实际开口的话题来源（新→旧），跳过无话题的兜底发言。"""
    from src.core.ase import _load_ase_state
    out: list = []
    for entry in reversed(_load_ase_state(storage_root).get("proactive_log") or []):
        s = entry.get("source")
        if s:
            out.append(s)
        if len(out) >= n:
            break
    return out


# 「向内」的来源：回忆与自身状态。连续开口都在其中时提示换换口味。
_INWARD_SOURCES = {"conversation_recall", "user_life", "ai_self"}


@register
class ReflectionTopicCandidatesSegment(PromptSegment):
    segment_id = "reflection_topic_candidates"
    inject_into = "reflection"
    is_readonly = True
    # is_core：本段是主动话题发现的机制本体，不开放单独禁用 —— 真正的开关是
    # topic_discovery.enabled（build() 内部据此 short-circuit），避免两个开关相互矛盾。
    is_core = True
    priority = 56
    label = "主动话题候选"
    description = "把多来源话题候选注入自省 prompt，供生成 topic_pick"

    def build(self, ctx) -> SegmentResult:
        if not isinstance(ctx, ReflectionBuildContext):
            return SegmentResult(messages=[])

        td_cfg = ctx.topic_discovery_cfg or {}
        if not td_cfg.get("enabled", True):
            return SegmentResult(messages=[])

        from src.core.topics import get_enabled_sources
        from src.core.topics.base import TopicSourceContext

        sources = get_enabled_sources(td_cfg)
        if not sources:
            return SegmentResult(messages=[])

        storage_root = os.path.join(get_project_root(), "profiles", ctx.profile_id)
        window = int(td_cfg.get("recent_used_window", 10))
        prev = ctx.prev_reflection or {}

        tctx = TopicSourceContext(
            profile_id=ctx.profile_id,
            storage_root=storage_root,
            profile=ctx.profile or {},
            config=td_cfg,
            recently_used=_recently_used_refs(storage_root, window),
            reflection_thought=(prev.get("thought") or ""),
            emotion=ctx.emotion,
            memory_manager=ctx.memory_manager,
        )

        # ai_self 选中冷却：上一轮已连选 ≥2 次（选中但一直没真正开口）时，本轮不再供其候选，
        # 否则自省会无限复读自己的 thought（实测 topic_pick 历史中 ai_self 占比畸高）
        prev_chosen = prev.get("chosen_topic") or {}
        skip_source_ids: set = set()
        if prev_chosen.get("source") == "ai_self" and int(prev_chosen.get("consec_picks", 1)) >= 2:
            skip_source_ids.add("ai_self")

        # 每个来源各自收集候选
        per_source: list[list] = []
        for src in sources:
            if src.source_id in skip_source_ids:
                per_source.append([])
                continue
            try:
                per_source.append(list(src.get_candidates(tctx) or []))
            except Exception as e:
                logger.debug("[topic_candidates] 来源 %s 取候选失败: %s", src.source_id, e)
                per_source.append([])

        # 跨来源轮转取样，保证各来源平等出现
        cap = max(1, int(td_cfg.get("candidate_cap", 8)))
        candidates: list = []
        idx = 0
        while len(candidates) < cap and any(idx < len(lst) for lst in per_source):
            for lst in per_source:
                if idx < len(lst):
                    candidates.append(lst[idx])
                    if len(candidates) >= cap:
                        break
            idx += 1

        ctx.topic_candidates = candidates
        if not candidates:
            return SegmentResult(messages=[])

        locale = ctx.locale or get_locale()
        if locale == "en":
            header = ("[Proactive Topic Candidates] Pick the one most worth raising right "
                      "now and put its id in topic_pick; if none is worth it, set topic_pick to null.")
            footer = ("If you picked a topic you genuinely want to share, that itself is grounds "
                      "to raise urgency. Put how you'd naturally bring it up in topic_angle. "
                      "Fresh outside topics (trends) are worth picking now and then even if they "
                      "differ from what you usually talk about — sharing news is its own kind of "
                      "closeness; don't dwell only on memories.")
        else:
            header = ("【可主动发起的话题候选】从中挑一个此刻最值得聊的，把它的 id 填进 topic_pick；"
                      "若都不值得提，topic_pick 填 null。")
            footer = ("如果你挑中了一个真心想分享的话题，这本身就是抬高 urgency 的理由。"
                      "topic_angle 里写你打算怎样自然切入。"
                      "外界的新鲜见闻（趋势类候选）即使和你们平时聊的不同，偶尔分享也是一种亲近，"
                      "不必总停留在回忆里。")

        lines = [header]
        for c in candidates:
            lines.append(f"- [{c.ref}] {c.summary}")
        lines.append("")
        lines.append(footer)

        # 轮换提示：最近几次开口都是「向内」的话题且本轮有外部候选时，明确鼓励换口味
        recent_sources = _recent_spoken_sources(storage_root, 2)
        has_external = any(not c.ref.startswith(tuple(f"{s}:" for s in _INWARD_SOURCES))
                           for c in candidates)
        if has_external and len(recent_sources) >= 2 and all(
                s in _INWARD_SOURCES for s in recent_sources):
            lines.append("")
            lines.append("Note: your last few initiations all drew on memories or your own state — "
                         "favor a fresh external topic this time."
                         if locale == "en" else
                         "提示：你最近几次主动开口都围绕回忆或你自己的状态，这次优先考虑新鲜的外部话题。")
        return SegmentResult(messages=[{"role": "system", "content": "\n".join(lines)}])
