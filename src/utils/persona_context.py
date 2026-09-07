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
    user_name: str = "",
) -> str:
    """返回 custom_prompt 正文；过长则截断。无配置时返回空字符串。

    从 SillyTavern 导入的人格，custom_prompt 里常含 {{char}}/{{user}}，
    这里与主对话一样展开，否则辅助模型会读到字面宏（并可能复制进分类结果）。
    """
    if not profile or not isinstance(profile, dict):
        return ""

    ref = profile.get("reflection_config") or {}
    if not isinstance(ref, dict):
        ref = {}

    text = (ref.get("custom_prompt") or "").strip()
    if not text:
        return ""

    from src.prompt.macros import build_macro_table, expand_macros
    _up = profile.get("user_persona") or {}
    text = expand_macros(text, build_macro_table(
        char_name=(profile.get("display_name") or ""),
        user_name=(user_name or _up.get("name") or "用户"),
        persona=(_up.get("introduction") or ""),
    ))

    if len(text) <= max_chars:
        return text

    return text[: max_chars - 1].rstrip() + "…"
