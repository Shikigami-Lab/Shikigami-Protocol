"""健康模式 segment — 公开版 overlay：默认视为开启（与私库源文件不同，仅写入公开同步目标目录）。"""
from __future__ import annotations

from src.config.prompt_loader import get_prompt, get_locale
from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register


@register
class HealthModeSegment(PromptSegment):
    segment_id = "health_mode"
    priority = 2  # 紧接 persona(0) 之后，优先于时间与记忆等上下文
    label = "健康模式"
    description = "启用后约束回复为全年龄友好、拒绝有害与违规内容，适用于对话与主动发言"
    is_core = False
    default_enabled = True  # 公开发行：默认开启；pipeline 在 public_mode 下亦强制启用
    inject_into = "chat"  # 普通聊天；ASE 走主路 prompt 已包含

    default_trigger_mode = "always"

    def build(self, ctx: BuildContext) -> SegmentResult:
        locale = get_locale()
        text = get_prompt("health_mode", locale=locale, default="")
        if not (text or "").strip():
            # 兜底（YAML 缺失时）
            text = (
                "[Healthy & safe reply mode] Keep replies family-friendly; refuse harmful or illegal requests; stay in character."
                if locale == "en"
                else "【健康模式】请保持全年龄友好回复，拒绝色情/暴力/违法与自残教唆等内容，语气仍符合角色。"
            )
        return SegmentResult(messages=[{"role": "system", "content": text.strip()}])
