"""与自省、情感分类、好感度调整等 secondary LLM 共用的人设片段（方案 A）。

唯一来源：reflection_config.custom_prompt（与 ReflectionEngine 的 persona_brief 同源）。
不包含 base_prompt，避免辅助调用过长或与分类任务噪声叠加。
"""
from __future__ import annotations

from typing import Any, Dict


def resolve_user_name(profile: Dict[str, Any] | None, app) -> str:
    """The user's name for helper-model prompts, in the chat pipeline's order: the profile's
    user persona, then the global user persona, then config.user_name ("" when none is known)."""
    cfg = getattr(getattr(app, "state", None), "config", None)
    profile_persona = (profile or {}).get("user_persona") or {}
    global_persona = (getattr(cfg, "user_persona", None) or {}) if cfg is not None else {}
    return str(
        profile_persona.get("name")
        or global_persona.get("name")
        or (getattr(cfg, "user_name", "") if cfg is not None else "")
        or ""
    ).strip()


def get_persona_context_for_secondary_llm(
    profile: Dict[str, Any] | None,
    *,
    max_chars: int = 3500,
    user_name: str = "",
) -> str:
    """Return the custom_prompt text, truncated if too long; empty string when not configured.

    Profiles imported from SillyTavern often have {{char}}/{{user}} in custom_prompt. Expand them
    the same way as the main chat, otherwise helper models read literal macros (and may copy them
    into classification results).
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
        user_name=(_up.get("name") or user_name or "用户"),
        persona=(_up.get("introduction") or ""),
    ))

    if len(text) <= max_chars:
        return text

    return text[: max_chars - 1].rstrip() + "…"
