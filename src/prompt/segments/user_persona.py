"""UserPersonaSegment — 将主角（用户）信息注入 system prompt。

优先级 2，紧接在 PersonaSegment(0) 之后。
数据来源（后者覆盖前者）：
  1. app.yaml 的 user_persona（全局）
  2. profile 的 user_persona（per-profile 局部覆盖）

新 schema：
  user_persona.name          — 称呼（AI 如何叫用户）
  user_persona.introduction  — 用户自我介绍（markdown）
  user_portrait.content      — AI 自动维护的画像（仅 per-profile）
"""
from __future__ import annotations

from typing import Any, Dict

from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register


def _resolve_intro_and_name(global_persona: Dict[str, Any], profile_persona: Dict[str, Any]) -> tuple:
    """Return (name, introduction). profile 字段优先于全局，留空继承。"""
    name = (profile_persona.get("name") or global_persona.get("name") or "").strip()
    introduction = (profile_persona.get("introduction") or global_persona.get("introduction") or "").strip()
    return name, introduction


@register
class UserPersonaSegment(PromptSegment):
    segment_id  = "user_persona"
    priority    = 2
    label       = "主角设定"
    description = "将主角（用户）的基本信息与 AI 形成的画像注入 system prompt"
    is_core     = False

    default_trigger_mode = "always"

    def build(self, ctx: BuildContext) -> SegmentResult:
        global_persona: Dict[str, Any] = ctx.extras.get("user_persona_global") or {}
        profile_persona: Dict[str, Any] = ctx.profile.get("user_persona") or {}
        portrait_block: Dict[str, Any] = ctx.profile.get("user_portrait") or {}
        portrait_cfg: Dict[str, Any] = ctx.profile.get("user_portrait_config") or {}

        name, introduction = _resolve_intro_and_name(global_persona, profile_persona)
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
