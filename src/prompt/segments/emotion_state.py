"""emotion_state segment — 注入 AI 当前多层情绪及其对应描述文本。

情绪融合规则（参照 emotion_system.py v2.0）：
  - 主情绪（layers[0]）：完整的 emotion_prompts 内容
  - 次情绪（layers[1]，intensity >= 0.25）：使用 emotion_zh_descriptions 的中文简述做融合提示
  - 三情绪（layers[2]，intensity >= 0.2）：三层融合完整描述
"""
import json
import logging
import os
from string import Template
from typing import Dict, List

from src.config.prompt_loader import get_prompt, get_locale
from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)


@register
class EmotionStateSegment(PromptSegment):
    segment_id = "emotion_state"
    priority = 65
    label = "情绪状态"
    description = "注入 AI 当前多层情绪名及其对应的角色描述文本，影响回复风格"
    is_core = False
    default_trigger_mode = "always"
    default_trigger_param = 1.0

    def build(self, ctx: BuildContext) -> SegmentResult:
        state_path = os.path.join(ctx.session.storage_root, "emotion_state.json")
        if not os.path.exists(state_path):
            return SegmentResult(fired=False)

        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            logger.warning("[emotion_state] failed to load state: %s", e)
            return SegmentResult(fired=False)

        # 优先使用 emotion_layers；若无则从 legacy 字段重建
        layers: List[Dict] = state.get("emotion_layers") or []
        if not layers:
            primary = state.get("primary_emotion", "calm")
            layers = [{"emotion": primary, "intensity": state.get("primary_weight", 1.0)}]
            if state.get("secondary_emotion"):
                layers.append({"emotion": state["secondary_emotion"],
                               "intensity": state.get("secondary_weight", 0.3)})
            if state.get("tertiary_emotion"):
                layers.append({"emotion": state["tertiary_emotion"],
                               "intensity": state.get("tertiary_weight", 0.2)})

        # 按强度降序排列，最多取 3 层
        layers = sorted(layers, key=lambda x: x.get("intensity", 0), reverse=True)[:3]
        if not layers:
            return SegmentResult(fired=False)

        prompts = _get_emotion_prompts(ctx.profile)
        zh_descs = _get_emotion_zh_descriptions(ctx.profile)
        if not prompts:
            return SegmentResult(fired=False)

        primary_emotion = layers[0]["emotion"]
        primary_lines = prompts.get(primary_emotion)
        if not primary_lines:
            return SegmentResult(fired=False)

        locale = get_locale()
        primary_zh = zh_descs.get(primary_emotion, primary_emotion)

        # 指令性 header — 提示 LLM 情绪状态是必须体现的约束
        if locale == "en":
            header = (
                f"[Current emotional state: {primary_zh} ({primary_emotion}) — "
                "the following emotional expression must be reflected in this response; do not respond with a flat or inconsistent tone]"
            )
        else:
            header = (
                f"【当前情绪状态：{primary_zh}（{primary_emotion}）— "
                "本轮回应必须体现以下情绪表达，不得以平淡或矛盾的语气回应】"
            )

        # 主情绪：完整 prompt 内容
        if isinstance(primary_lines, list):
            content = header + "\n" + "\n".join(primary_lines)
        else:
            content = header + "\n" + str(primary_lines)

        # 次情绪融合提示（intensity >= 0.25）
        if len(layers) >= 2 and layers[1].get("intensity", 0) >= 0.25:
            secondary = layers[1]["emotion"]
            secondary_intensity = layers[1]["intensity"]
            secondary_zh = zh_descs.get(secondary, secondary)
            default_fusion_triple = (
                "[Multi-layer Emotion Blend]\n"
                "- Layer 1: ${primary_zh} (intensity ${intensity_0})\n"
                "- Layer 2: ${secondary_zh} (intensity ${secondary_intensity})\n"
                "- Layer 3: ${tertiary_zh} (intensity ${tertiary_intensity})\n\n"
                "Blending guide: with ${primary_zh} as the core, blend in ${secondary_zh} "
                "(${secondary_intensity}) and ${tertiary_zh} (${tertiary_intensity}) to form a complete, "
                "harmonious whole — the primary emotion provides direction, the supporting emotions add color "
                "and depth, creating natural and genuine expression."
                if locale == "en"
                else "【多层情感融合】\n- 第1层：${primary_zh}（强度${intensity_0}）\n- 第2层：${secondary_zh}（强度${secondary_intensity}）\n- 第3层：${tertiary_zh}（强度${tertiary_intensity}）\n\n融合指导：以${primary_zh}为核心，融合${secondary_zh}（${secondary_intensity}）和${tertiary_zh}（${tertiary_intensity}），让这三层情感形成一个完整而和谐的整体——主情感提供方向，辅助情感添加色彩和深度，形成自然而真实的表达。"
            )
            default_fusion_dual = (
                "[Emotion Blend]\nWith ${primary_zh} as the foundation, blend in the color of ${secondary_zh} — "
                "maintain the character of the primary emotion while letting the warmth of the secondary show through."
                if locale == "en"
                else "【情感融合】\n在${primary_zh}的基调下，混入${secondary_zh}的色彩，使表达既保持主要情感的特征，又体现次要情感的温度。"
            )
            if len(layers) >= 3 and layers[2].get("intensity", 0) >= 0.2:
                tertiary = layers[2]["emotion"]
                tertiary_intensity = layers[2]["intensity"]
                tertiary_zh = zh_descs.get(tertiary, tertiary)
                fusion = "\n\n" + Template(get_prompt(
                    "fusion_triple", locale=locale,
                    default=default_fusion_triple,
                )).safe_substitute(
                    primary_zh=primary_zh,
                    intensity_0=f"{layers[0]['intensity']:.0%}",
                    secondary_zh=secondary_zh,
                    secondary_intensity=f"{secondary_intensity:.0%}",
                    tertiary_zh=tertiary_zh,
                    tertiary_intensity=f"{tertiary_intensity:.0%}",
                )
            else:
                fusion = "\n\n" + Template(get_prompt(
                    "fusion_dual", locale=locale,
                    default=default_fusion_dual,
                )).safe_substitute(
                    primary_zh=primary_zh,
                    secondary_zh=secondary_zh,
                )
            content += fusion

        return SegmentResult(messages=[{"role": "system", "content": content}])


def _get_emotion_prompts(profile: dict) -> dict:
    """从 profile 或 YAML 获取 emotion_prompts。"""
    from src.config.prompt_loader import get_dict, get_locale
    prompts = profile.get("emotion_config", {}).get("emotion_prompts") or {}
    if prompts:
        return prompts
    return get_dict("emotion_prompts", locale=get_locale())


def _get_emotion_zh_descriptions(profile: dict) -> dict:
    """从 profile 或 YAML 获取 emotion_zh_descriptions（简述词）。"""
    from src.config.prompt_loader import get_dict, get_locale
    descs = profile.get("emotion_config", {}).get("emotion_zh_descriptions") or {}
    if descs:
        return descs
    return get_dict("zh_descriptions", locale=get_locale())
