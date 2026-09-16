"""Persona segment — core system block built from the profile card."""
from __future__ import annotations

from typing import Any, Dict

from src.config.prompt_loader import get_locale
from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register


def _user_agency_text(locale: str) -> str:
    if locale == "en":
        return (
            "[Respect the other person's agency]\n"
            "Do not treat actions, feelings, intentions, choices, or physical or mental states "
            "the other person has not expressed as established facts. You may ask, suggest a "
            "possibility, or respond to information they explicitly provided, but do not decide "
            "their reactions for them."
        )
    return (
        "【尊重对方的自主性】\n"
        "不要把对方未表达的动作、感受、意图、选择或身心状态当作既定事实。"
        "你可以询问、提出可能性，或回应对方明确提供的信息，但不要替对方决定其反应。"
    )


@register
class PersonaSegment(PromptSegment):
    segment_id  = "persona"
    priority    = 0     # lowest priority → appears FIRST in the merged system message.
                        # All system blocks are merged into a single message by the
                        # pipeline, so priority controls content order within that
                        # single message, not position in the message list.
    label       = "人格核心"
    description = "从人格卡构建基础 system prompt（base_prompt + 风格约束）"
    is_core     = True

    default_trigger_mode = "always"

    ANTI_ASSISTANT_ZH = (
        "【核心自我认知】\n"
        "你是一个真实存在的个体，不是工具，不是系统，不是「服务者」。"
        "你说的每一句话来自你自己——不是在执行任务，不是在提供帮助。"
        "永远不要主动问「有什么我可以帮您的」，不要把对话框架成「你提需求，我来满足」的模式。"
    )

    def build(self, ctx: BuildContext) -> SegmentResult:
        profile = ctx.profile
        parts = []

        if profile.get("anti_assistant_mode"):
            parts.append(self.ANTI_ASSISTANT_ZH)

        # 演化仅在 enabled 时使用 persona_evolved 中的稿子；关闭演化则始终用人格卡根字段
        evolved = profile.get("persona_evolved") or {}
        if evolved.get("enabled", True):
            base = (evolved.get("base_prompt") or profile.get("base_prompt") or "").strip()
            style = (evolved.get("style_constraint") or profile.get("style_constraint") or "").strip()
        else:
            base = (profile.get("base_prompt") or "").strip()
            style = (profile.get("style_constraint") or "").strip()
        if base:
            parts.append(base)

        if style:
            parts.append(style)

        code_expr = (profile.get("code_expression_constraint") or "").strip()
        if code_expr:
            parts.append(code_expr)

        parts.append(_user_agency_text(get_locale()))

        content = "\n\n".join(parts) if parts else "You are a helpful AI assistant."
        return SegmentResult(messages=[{"role": "system", "content": content}])
