"""与自省、情感分类、好感度调整等 secondary LLM 共用的人设片段（方案 A）。

唯一来源：reflection_config.custom_prompt（与 ReflectionEngine 的 persona_brief 同源）。
不包含 base_prompt，避免辅助调用过长或与分类任务噪声叠加。
"""
from __future__ import annotations

from typing import Any, Dict


def get_persona_context_for_secondary_llm(
    profile: Dict[str, Any] | None,
    *,
    max_chars: int = 3500,
) -> str:
    """返回 custom_prompt 正文；过长则截断。无配置时返回空字符串。"""
    if not profile or not isinstance(profile, dict):
        return ""

    ref = profile.get("reflection_config") or {}
    if not isinstance(ref, dict):
        ref = {}

    text = (ref.get("custom_prompt") or "").strip()
    if not text:
        return ""

    if len(text) <= max_chars:
        return text

    return text[: max_chars - 1].rstrip() + "…"
