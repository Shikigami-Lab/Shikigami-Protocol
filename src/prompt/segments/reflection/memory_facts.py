"""reflection_memory_facts — Long-term memory injection for reflection.

Priority 70, inject_into="reflection".

Fetches high-weight facts from MemoryManager and injects them as optional
conversation seeds, creating the "how did she remember that?" surprise effect.
"""
from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_locale


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
            facts = mm.get_reflection_context_facts(max_count=4, min_weight=0.6)
        except Exception:
            return SegmentResult(messages=[])

        if not facts:
            return SegmentResult(messages=[])

        locale = ctx.locale or get_locale()

        if locale == "en":
            header = "[Past Memories Available for Reference — use selectively, don't force]"
            footer = "If any of the above contains a detail worth naturally bringing up right now, use it as a conversation starting point."
            weight_label = "weight"
        else:
            header = "【可供引用的过往记忆（选择性使用，不要强行提及）】"
            footer = "如果以上记忆中有值得在此刻自然提起的细节，可将其作为话题出发点，以 memory_recall 为动机开口。"
            weight_label = "权重"

        lines = [header]
        for f in facts:
            lines.append(f"- [{f['date']}] {f['content']} ({weight_label}: {f['weight']})")
        lines.append("")
        lines.append(footer)

        content = "\n".join(lines)
        return SegmentResult(messages=[{"role": "system", "content": content}])
