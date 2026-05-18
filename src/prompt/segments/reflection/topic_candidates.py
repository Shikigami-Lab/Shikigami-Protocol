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

        # 每个来源各自收集候选
        per_source: list[list] = []
        for src in sources:
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
                      "to raise urgency. Put how you'd naturally bring it up in topic_angle.")
        else:
            header = ("【可主动发起的话题候选】从中挑一个此刻最值得聊的，把它的 id 填进 topic_pick；"
                      "若都不值得提，topic_pick 填 null。")
            footer = ("如果你挑中了一个真心想分享的话题，这本身就是抬高 urgency 的理由。"
                      "topic_angle 里写你打算怎样自然切入。")

        lines = [header]
        for c in candidates:
            lines.append(f"- [{c.ref}] {c.summary}")
        lines.append("")
        lines.append(footer)
        return SegmentResult(messages=[{"role": "system", "content": "\n".join(lines)}])
