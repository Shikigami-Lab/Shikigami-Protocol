"""Health mode segment: keeps replies family-friendly and refuses harmful requests."""
from __future__ import annotations

from src.config.prompt_loader import get_prompt, get_locale
from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register


@register
class HealthModeSegment(PromptSegment):
    segment_id = "health_mode"
    priority = 2  # right after persona (0), ahead of time and memory context
    label = "健康模式"
    description = "启用后约束回复为全年龄友好、拒绝有害与违规内容，适用于对话与主动发言"
    is_core = False
    default_enabled = True
    inject_into = "chat"  # regular chat; the ASE prompt is built through the chat path as well

    default_trigger_mode = "always"

    def build(self, ctx: BuildContext) -> SegmentResult:
        locale = get_locale()
        text = get_prompt("health_mode", locale=locale, default="")
        if not (text or "").strip():
            # Fallback when the YAML prompt is missing
            text = (
                "[Healthy & safe reply mode] Keep replies family-friendly; refuse harmful or illegal requests; stay in character."
                if locale == "en"
                else "【健康模式】请保持全年龄友好回复，拒绝色情/暴力/违法与自残教唆等内容，语气仍符合角色。"
            )
        return SegmentResult(messages=[{"role": "system", "content": text.strip()}])
