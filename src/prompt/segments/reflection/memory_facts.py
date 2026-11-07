"""reflection_memory_facts — Long-term memory injection for reflection.

Priority 70, inject_into="reflection".

Fetches high-weight facts from MemoryManager and injects them as optional
conversation seeds, creating the "how did she remember that?" surprise effect.
"""
from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_locale
from src.utils.memory_time import read_time_notice


@register
class ReflectionMemoryFactsSegment(PromptSegment):
    segment_id = "reflection_memory_facts"
    inject_into = "reflection"
    is_readonly = True
    is_core = False
    default_enabled = True
    priority = 70
    label = "记忆引用（惊喜感）"
    description = "让自省时引用长期记忆，产生「她怎么记得」的效果"

    def build(self, ctx) -> SegmentResult:
        if not isinstance(ctx, ReflectionBuildContext):
            return SegmentResult(messages=[])

        mm = ctx.memory_manager
        if mm is None:
            return SegmentResult(messages=[])

        try:
            recent_user_text = [turn.get("content", "")[:300] for turn in ctx.recent_turns
                                if turn.get("role") == "user"]
            facts = mm.get_reflection_context_facts(
                max_count=4, min_weight=0.6, query="\n".join(recent_user_text[-3:]),
            )
        except Exception:
            return SegmentResult(messages=[])

        if not facts:
            return SegmentResult(messages=[])

        locale = ctx.locale or get_locale()

        if locale == "en":
            header = "[Past Memories Available for Reference — use selectively, don't force]"
            footer = ("Use these notes to understand the user. For an unsolicited recollection of a past event, "
                      "choose an evidence-backed Conversation Recall candidate instead.")
            weight_label = "weight"
        else:
            header = "【可供引用的过往记忆（选择性使用，不要强行提及）】"
            footer = "这些事实用于理解用户。想主动提起一件旧事时，请选择有原话依据的「对话回忆」候选。"
            weight_label = "权重"

        lines = [header, read_time_notice(locale)]
        for f in facts:
            date_label = 'record updated' if locale == 'en' else '记录更新日'
            lines.append(f"- [{date_label}: {f['date']}] {f['content']} ({weight_label}: {f['weight']})")
        lines.append("")
        lines.append(footer)

        content = "\n".join(lines)
        return SegmentResult(messages=[{"role": "system", "content": content}])
