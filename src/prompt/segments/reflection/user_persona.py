"""reflection_user_persona — 向自省引擎注入主角（用户）信息。

让 AI 在内心独白时用真实姓名和角色认知来思念或描述用户，而非泛指「用户」。
优先级 1，紧跟自省人格 (priority=0) 之后。
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
    description  = "向自省引擎注入主角的姓名和角色定位，使内心独白更具体"

    def build(self, ctx) -> SegmentResult:
        if not isinstance(ctx, ReflectionBuildContext):
            return SegmentResult(messages=[])

        up = ctx.user_persona or {}
        name        = (up.get("name")          or ctx.user_name or "").strip()
        description = (up.get("description")   or "").strip()
        personality = (up.get("personality")   or "").strip()
        role        = (up.get("role_in_story") or "").strip()

        if not any([name, description, personality, role]):
            return SegmentResult(messages=[])

        lines = ["【你所认识的人】"]
        if name:
            lines.append(f"他叫{name}。")
        if description:
            lines.append(description)
        if personality:
            lines.append(personality)
        if role:
            lines.append(role)

        return SegmentResult(
            messages=[{"role": "system", "content": "\n".join(lines)}]
        )
