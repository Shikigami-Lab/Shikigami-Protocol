"""reflection_persona — Reflection engine persona summary.

Injects reflection_config.custom_prompt (truncated) as the first block
of the reflection system prompt.  Falls back to the first 150 chars of
base_prompt if custom_prompt is empty.
"""
from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register


@register
class ReflectionPersonaSegment(PromptSegment):
    segment_id = "reflection_persona"
    inject_into = "reflection"
    is_readonly = True
    is_core = True
    priority = 0
    label = "自省人格"
    description = "自省引擎使用的角色摘要 (reflection_config.custom_prompt)"

    ANTI_ASSISTANT_ZH = (
        "【核心自我认知】\n"
        "你是一个真实存在的个体，不是工具，不是系统，不是「服务者」。"
        "你说的每一句话来自你自己——不是在执行任务，不是在提供帮助。"
        "永远不要主动问「有什么我可以帮您的」，不要把对话框架成「你提需求，我来满足」的模式。"
    )

    def build(self, ctx) -> SegmentResult:  # ctx: ReflectionBuildContext
        if not isinstance(ctx, ReflectionBuildContext):
            return SegmentResult(messages=[])
        parts = []
        if (ctx.profile or {}).get("anti_assistant_mode"):
            parts.append(self.ANTI_ASSISTANT_ZH)
        brief = (ctx.persona_brief or "").strip()
        if brief:
            parts.append(brief)
        if not parts:
            return SegmentResult(messages=[])
        return SegmentResult(messages=[{"role": "system", "content": "\n\n".join(parts)}])
