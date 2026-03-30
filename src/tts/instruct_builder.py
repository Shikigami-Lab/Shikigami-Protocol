"""
TTS instruct 兼容层：情绪 → instruct，预留扩展（语速、音高、自定义指令等）。

供 Qwen3-TTS 等支持自然语言 instruct 的引擎使用：将当前情绪、后续可扩展的 speed/pitch 等
合成为一句「怎么说」的指令，与人格配置的固定 instruct 拼接后传给 TTS。

情绪描述从 config/prompts/emotion.yaml 的 zh_descriptions 读取（通过 prompt_loader）。
"""
import logging
from typing import Dict, Optional

from src.config.prompt_loader import get_dict, get_locale

logger = logging.getLogger(__name__)


def build_instruct(
    emotion: Optional[str] = None,
    profile_emotion_zh_descriptions: Optional[Dict[str, str]] = None,
    base_instruct: Optional[str] = None,
    *,
    speed_hint: Optional[str] = None,
    pitch_hint: Optional[str] = None,
    extra: Optional[str] = None,
) -> str:
    """
    合成一条 TTS 用 instruct 字符串，兼容未来扩展。

    - emotion: 当前主情绪（英文 key，如 joyful / calm），会转为「用xxx的语气说」
    - profile_emotion_zh_descriptions: 人格的 emotion_zh_descriptions，有则优先用
    - base_instruct: 人格或全局配置的固定 instruct，会放在最前
    - speed_hint: 预留，如 "slow" / "fast"
    - pitch_hint: 预留，如 "high" / "low"
    - extra: 预留，任意追加说明

    Returns:
        拼接后的 instruct，可直接传给 Qwen3-TTS 的 instruct 参数。
    """
    parts = []
    if base_instruct and str(base_instruct).strip():
        parts.append(str(base_instruct).strip())

    emotion_desc = ""
    if emotion and str(emotion).strip().lower() != "calm":
        key = str(emotion).strip().lower()
        if profile_emotion_zh_descriptions and key in profile_emotion_zh_descriptions:
            emotion_desc = profile_emotion_zh_descriptions[key]
        else:
            defaults = get_dict("zh_descriptions", locale=get_locale())
            emotion_desc = defaults.get(key, "")
        if emotion_desc:
            parts.append(f"用{emotion_desc}的语气说")

    if speed_hint and str(speed_hint).strip():
        s = str(speed_hint).strip().lower()
        if s == "slow":
            parts.append("用较慢的语速说")
        elif s == "fast":
            parts.append("用较快的语速说")

    if pitch_hint and str(pitch_hint).strip():
        p = str(pitch_hint).strip().lower()
        if p == "high":
            parts.append("音调偏高")
        elif p == "low":
            parts.append("音调偏低")

    if extra and str(extra).strip():
        parts.append(str(extra).strip())

    return "。".join(parts) if parts else ""
