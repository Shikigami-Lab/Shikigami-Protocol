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

import time
from typing import Any, Dict

from src.config.prompt_loader import get_locale
from src.core.user_portrait import (
    extract_style_address_form,
    format_mutual_address_facts,
    recent_context_status,
    recent_context_ttl_hours,
)
from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register


def _get_fresh_recent_context(
    portrait_block: Dict[str, Any],
    ttl_hours: float,
    now_ts: float,
) -> str:
    """Recent context to send, or "" once expired (the same rule the settings panel shows)."""
    status = recent_context_status(portrait_block, ttl_hours, now_ts)
    return "" if status["expired"] else status["content"]


def _matching_contextual_details(
    portrait_block: Dict[str, Any],
    scan_text: str,
    max_items: int = 3,
) -> list[str]:
    haystack = (scan_text or "").casefold()
    if not haystack:
        return []
    matched: list[str] = []
    for item in portrait_block.get("contextual_details") or []:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        keywords = [
            str(keyword or "").strip().casefold()
            for keyword in item.get("keywords") or []
            if str(keyword or "").strip()
        ]
        if summary and any(keyword in haystack for keyword in keywords):
            matched.append(summary)
            if len(matched) >= max_items:
                break
    return matched


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
        # Only an explicitly set name is used as the user's name. Without one, a form of
        # address from the style rules may still be stated, but never as a name.
        style_address_form = "" if name else extract_style_address_form(ctx.profile)
        portrait_enabled = portrait_cfg.get("enabled", True)
        portrait_text = (portrait_block.get("content") or "").strip() if portrait_enabled else ""
        recent_context = ""
        contextual_details: list[str] = []
        if portrait_enabled:
            recent_context = _get_fresh_recent_context(
                portrait_block,
                ttl_hours=recent_context_ttl_hours(portrait_cfg),
                now_ts=time.time(),
            )
            scan_parts = [ctx.user_msg or ""]
            try:
                scan_parts.extend(
                    str(turn.get("content") or "")
                    for turn in ctx.store.get_recent(4)
                )
            except Exception:
                pass
            contextual_details = _matching_contextual_details(
                portrait_block,
                "\n".join(scan_parts),
            )

        # Mutual address goes on its own lines, not inside the portrait prose: the address
        # table is kept exact, while the portrait is rewritten on every refresh.
        address_line = ""
        if portrait_enabled and portrait_cfg.get("address_forms_enabled", True):
            address_line = format_mutual_address_facts(
                name,
                portrait_block,
                locale=get_locale(),
                style_address_form=style_address_form,
            )
        elif name or style_address_form:
            # With the portrait off, still state how the character addresses the user.
            address_line = format_mutual_address_facts(
                name, {}, locale=get_locale(), style_address_form=style_address_form,
            )

        if not any([
            name,
            introduction,
            portrait_text,
            address_line,
            recent_context,
            contextual_details,
        ]):
            return SegmentResult(messages=[])

        locale = get_locale()
        lines = ["[Person You Know]" if locale == "en" else "【你所认识的人】"]
        if name:
            lines.append(f"他叫{name}。" if locale != "en" else f"Their name/preferred address is {name}.")
        if introduction:
            lines.append(introduction)
        if address_line:
            lines.append(address_line)

        if portrait_text:
            lines.append("")
            lines.append("[Stable Impression]" if locale == "en" else "【你对他的稳定印象】")
            lines.append(portrait_text)
        if recent_context:
            lines.append("")
            lines.append("[Recent Context]" if locale == "en" else "【近期状态】")
            lines.append(recent_context)
        if contextual_details:
            lines.append("")
            lines.append("[Relevant Details]" if locale == "en" else "【与当前话题相关的细节】")
            lines.extend(f"- {detail}" for detail in contextual_details)

        return SegmentResult(
            messages=[{"role": "system", "content": "\n".join(lines)}]
        )
