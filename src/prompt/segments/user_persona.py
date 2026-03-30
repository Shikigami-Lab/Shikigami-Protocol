"""UserPersonaSegment — 将主角（用户）信息注入 system prompt。

优先级 2，紧接在 PersonaSegment(0) 之后。
数据来源（后者覆盖前者）：
  1. app.yaml 的 user_persona（全局）
  2. profile 的 user_persona（per-profile 局部覆盖）
"""
from __future__ import annotations

from typing import Any, Dict

from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register


@register
class UserPersonaSegment(PromptSegment):
    segment_id  = "user_persona"
    priority    = 2
    label       = "主角设定"
    description = "将主角（用户）的基本信息注入 system prompt（全局设定 + 人格局部覆盖）"
    is_core     = False

    default_trigger_mode = "always"

    def build(self, ctx: BuildContext) -> SegmentResult:
        # 全局设定
        global_persona: Dict[str, Any] = ctx.extras.get("user_persona_global") or {}
        # per-profile 覆盖
        profile_persona: Dict[str, Any] = ctx.profile.get("user_persona") or {}

        # 合并：profile 的值覆盖全局
        name        = (profile_persona.get("name")        or global_persona.get("name")        or "").strip()
        description = (profile_persona.get("description") or global_persona.get("description") or "").strip()
        personality = (profile_persona.get("personality") or global_persona.get("personality") or "").strip()
        role        = (profile_persona.get("role_in_story")                                    or "").strip()

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
