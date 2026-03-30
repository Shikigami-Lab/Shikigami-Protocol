"""reflection_affinity — Affinity state for reflection.

Injects the current affinity status into the reflection prompt so the
reflection LLM can calibrate urgency/tone based on relationship depth.
"""
from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_locale, render


@register
class ReflectionAffinitySegment(PromptSegment):
    segment_id = "reflection_affinity"
    inject_into = "reflection"
    is_readonly = True
    is_core = False
    priority = 40
    label = "好感度状态"
    description = "向自省引擎注入当前好感度（affinity_state.json）"

    def build(self, ctx) -> SegmentResult:  # ctx: ReflectionBuildContext
        if not isinstance(ctx, ReflectionBuildContext):
            return SegmentResult(messages=[])
        affinity = ctx.affinity
        if not affinity:
            return SegmentResult(messages=[])
        locale = ctx.locale or get_locale()
        status = affinity.get("status", "")
        if not status:
            return SegmentResult(messages=[])
        content = render("reflection.affinity_label", locale=locale,
                         default=("[Affinity] ($status)" if locale == "en"
                                  else "【好感度】（$status）"), status=status)
        return SegmentResult(messages=[{"role": "system", "content": content}])
