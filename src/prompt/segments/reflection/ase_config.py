"""reflection_ase_config — Injects ASE decision parameters into reflection.

Tells the reflection LLM about the current ASE mode and urgency threshold
so it can calibrate its urgency output accordingly.
"""
from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_locale, render


@register
class ReflectionAseConfigSegment(PromptSegment):
    segment_id = "reflection_ase_config"
    inject_into = "reflection"
    is_readonly = True
    is_core = False
    priority = 10
    label = "ASE决策参数"
    description = "向自省引擎注入 ASE 模式与 urgency 阈值，帮助校准 urgency 输出"

    def build(self, ctx) -> SegmentResult:  # ctx: ReflectionBuildContext
        if not isinstance(ctx, ReflectionBuildContext):
            return SegmentResult(messages=[])
        adc = ctx.ase_decision_context
        if not adc:
            return SegmentResult(messages=[])
        locale = ctx.locale or get_locale()
        mode_name = adc.get("mode_name", "medium")
        threshold = adc.get("urgency_threshold", 0.35)
        content = render(
            "reflection.ase_section",
            locale=locale,
            mode_name=mode_name,
            threshold=threshold,
            default=(
                f"[ASE Decision] Mode: {mode_name}; consider proactive speech when urgency ≥ {threshold}"
                if locale == "en"
                else f"【ASE 决策】模式：{mode_name}；urgency ≥ {threshold} 时考虑主动发言"
            ),
        )
        if not content.strip():
            return SegmentResult(messages=[])
        return SegmentResult(messages=[{"role": "system", "content": content}])
