"""reflection_topic_candidates —— 主动话题候选注入（自省）。

遍历启用的主动话题来源收集候选，
跨来源轮转取样保证「平等」，注入自省 prompt 供 LLM 用 topic_pick 选定一个。
候选连同复合 ID 一并暴露在 ctx.topic_candidates，供 _reflect() 解析 topic_pick。

inject_into="reflection"，priority=56（persona 类段落之后）。
"""
import logging
import os
import time

from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_locale
from src.utils.paths import get_project_root

logger = logging.getLogger(__name__)


_RECALL_COOLDOWN = 14 * 86400


def _recently_used_refs(storage_root: str, window: int) -> set:
    from src.core.ase import _load_ase_state

    state = _load_ase_state(storage_root)
    refs = {
        f"{entry['source']}:{entry['item_id']}"
        for entry in (state.get("proactive_log") or [])[-max(1, window):]
        if entry.get("source") and entry.get("item_id")
    }
    cutoff = time.time() - _RECALL_COOLDOWN
    refs.update(f"conversation_recall:{i}" for i, ts in (state.get("recall_spoken") or {}).items()
                if isinstance(ts, (int, float)) and ts > cutoff)
    return refs


def remember_recall_spoken(state, item_id, now):
    cutoff = now - _RECALL_COOLDOWN
    state["recall_spoken"] = {
        i:ts for i,ts in (state.get("recall_spoken") or {}).items()
        if isinstance(ts, (int,float)) and ts > cutoff
    }
    state["recall_spoken"][item_id] = now


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

        sources = [source for source in get_enabled_sources(td_cfg) if source.source_id != "user_life"]
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
            session=ctx.session,
            app=ctx.app,
        )

        # ai_self pick cooldown: once it was picked ≥2 rounds in a row (picked but never actually spoken),
        # leave it out this round; otherwise reflection keeps repeating its own thought and ai_self
        # dominates the picks
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
            footer = ("Use topic_angle for how to bring it up. For a previous event, "
                      "only use a Conversation Recall candidate. Treat its quotation as evidence of what "
                      "the user said; never treat a plan as a completed event.")
        else:
            header = ("【可主动发起的话题候选】从中挑一个此刻最值得聊的，把它的 id 填进 topic_pick；"
                      "若都不值得提，topic_pick 填 null。")
            footer = ("topic_angle 写打算怎样自然切入。想主动回忆过去的事，只选「对话回忆」候选。"
                      "原话只证明用户当时说了什么，不能把计划说成已经发生。")

        lines = [header]
        for c in candidates:
            lines.append(f"- [{c.ref}] {c.summary}")
        lines.append("")
        lines.append(footer)

        return SegmentResult(messages=[{"role": "system", "content": "\n".join(lines)}])
