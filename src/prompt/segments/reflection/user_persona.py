"""reflection_user_persona — 向自省引擎注入主角（用户）信息。

让 AI 在内心独白时用真实姓名和角色认知来思念或描述用户，而非泛指「用户」。
优先级 1，紧跟自省人格 (priority=0) 之后。

新 schema：name + introduction + portrait（来自 ReflectionBuildContext.user_persona/profile）
"""
from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register


@register
class ReflectionUserPersonaSegment(PromptSegment):
    segment_id   = "reflection_user_persona"
    inject_into  = "reflection"
    is_readonly  = True
    is_core      = False
    priority     = 1
    label        = "主角信息（自省）"
    description  = "向自省引擎注入主角姓名、用户自我介绍与 AI 画像"

    def build(self, ctx) -> SegmentResult:
        if not isinstance(ctx, ReflectionBuildContext):
            return SegmentResult(messages=[])

        up = ctx.user_persona or {}
        name = (up.get("name") or ctx.user_name or "").strip()
        introduction = (up.get("introduction") or "").strip()

        profile = getattr(ctx, "profile", None) or {}
        portrait_block = profile.get("user_portrait") or {}
        portrait_cfg = profile.get("user_portrait_config") or {}
        portrait_enabled = portrait_cfg.get("enabled", True)
        portrait_text = (portrait_block.get("content") or "").strip() if portrait_enabled else ""

        if not any([name, introduction, portrait_text]):
            return SegmentResult(messages=[])

        lines = ["【你所认识的人】"]
        if name:
            lines.append(f"他叫{name}。")
        if introduction:
            lines.append(introduction)

        if portrait_text:
            lines.append("")
            lines.append("【你对他的整体印象】")
            lines.append(portrait_text)

        return SegmentResult(
            messages=[{"role": "system", "content": "\n".join(lines)}]
        )
