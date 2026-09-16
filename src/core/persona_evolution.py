"""persona_evolution.py — 人格演化引擎

职责：
  - extract_anchor : 从 base_prompt + style_constraint 提炼 core_anchor（供 autofill/wizard 调用）
  - trigger_evolution : 记忆刷新后调用，执行一次演化重构，写入 profile + changelog
  - get_changelog : 读取演化历史
  - rollback : 回滚到指定版本

数据存储：
  - profiles/<id>.json["persona_evolved"]           — 当前演化状态（工作版本）
  - profiles/<id>/persona_changelog.json            — 演化历史（最近 MAX_CHANGELOG_ENTRIES 条）
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import traceback
from string import Template
from typing import Any, Dict, List, Optional

from src.config.prompt_loader import get_prompt, get_locale
from src.utils.debug_logger import log_secondary_llm_call, log_secondary_llm_response, log_error
from src.utils.paths import get_project_root

logger = logging.getLogger(__name__)

_PROFILES_DIR = os.path.join(get_project_root(), "profiles")
_CHANGELOG_FILE = "persona_changelog.json"
MAX_CHANGELOG_ENTRIES = 20  # 最多保留最近 N 条演化记录
DEFAULT_BASELINE_REVIEW_INTERVAL = 3  # every Nth evolution rebuilds on the original persona; 0 = off


def _is_baseline_review(evolution_count: int, interval: int) -> bool:
    """Return whether the next completed evolution is a baseline-review round."""
    if interval <= 0:
        return False
    return (max(0, evolution_count) + 1) % interval == 0


# A heading is a short label on its own line: 【核心身份】, [Core Identity] or "## Behavior".
# Bracketed lines with separators or macros are content rather than headings, e.g. SillyTavern
# PList blocks ("[Mochi's persona: proud, lazy; ...]"), which evolution is allowed to edit.
_BRACKET_HEADING_RE = re.compile(r"【([^】\r\n]{1,24})】|\[([^\]\r\n]{1,24})\]")
_HEADING_SEPARATORS = frozenset(":：;；,，=")


def _extract_section_headings(text: str) -> List[str]:
    """Extract explicit persona section headings while preserving their spelling."""
    headings: List[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if "{{" in line:
            continue  # the model may write the expanded name instead of the macro
        bracket = _BRACKET_HEADING_RE.fullmatch(line)
        if bracket:
            label = bracket.group(1) or bracket.group(2) or ""
            if not any(ch in _HEADING_SEPARATORS for ch in label):
                headings.append(line)
        elif re.fullmatch(r"#{1,4}\s+\S.*", line):
            headings.append(line)
    return headings


def _validate_evolution_output(
    original_base: str,
    new_base: str,
    current_style: str,
    new_style: str,
) -> tuple[bool, str, str]:
    """Validate structural continuity and resolve an omitted style safely."""
    candidate = (new_base or "").strip()
    if not candidate:
        return False, "", "base_prompt is empty"

    missing = [
        heading for heading in _extract_section_headings(original_base)
        if heading not in candidate
    ]
    if missing:
        return False, "", f"missing original sections: {', '.join(missing)}"

    effective_style = (new_style or "").strip() or (current_style or "").strip()
    return True, effective_style, ""


# ── 路径工具 ──────────────────────────────────────────────────────────────────

def _profile_card_path(profile_id: str) -> str:
    return os.path.join(_PROFILES_DIR, f"{profile_id}.json")


def _changelog_path(storage_root: str) -> str:
    return os.path.join(storage_root, _CHANGELOG_FILE)


def _load_profile_card(profile_id: str) -> Dict[str, Any]:
    path = _profile_card_path(profile_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("[PersonaEvolution] 读取 profile card 失败 %s: %s", profile_id, e)
        return {}


def _save_profile_card(profile_id: str, card: Dict[str, Any]) -> None:
    # 原子写：人格卡是角色的核心文件，崩溃在写入中途会损坏整个角色
    path = _profile_card_path(profile_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _update_persona_evolved(profile_id: str, fields: Dict[str, Any]) -> bool:
    """Merge fields into persona_evolved of the latest saved card.

    The card is reloaded right before writing, so edits saved while an LLM call was running
    (profile settings, a portrait refresh) are kept. Returns False without writing when the
    profile is gone or evolution was switched off in the meantime.
    """
    latest = _load_profile_card(profile_id)
    if not latest:
        return False
    evolved = latest.get("persona_evolved") or {}
    if not evolved.get("enabled", True):
        return False
    evolved.update(fields)
    latest["persona_evolved"] = evolved
    _save_profile_card(profile_id, latest)
    return True


# ── Changelog I/O ─────────────────────────────────────────────────────────────

def get_changelog(storage_root: str) -> List[Dict[str, Any]]:
    """返回演化历史列表（最新在前）。"""
    path = _changelog_path(storage_root)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("[PersonaEvolution] 读取 changelog 失败: %s", e)
        return []


def _append_changelog(storage_root: str, entry: Dict[str, Any]) -> None:
    entries = get_changelog(storage_root)
    entries.insert(0, entry)
    entries = entries[:MAX_CHANGELOG_ENTRIES]
    path = _changelog_path(storage_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


# ── LLM 调用辅助 ──────────────────────────────────────────────────────────────

def _parse_json_object(raw: str) -> Dict[str, Any]:
    """Return the JSON object in an LLM reply.

    Code fences, prose around the JSON and raw newlines inside string values are tolerated
    (a rewritten base_prompt is long multi-line text). Raises ValueError when there is none.
    """
    from src.core.user_portrait import _iter_json_values
    for value in _iter_json_values(raw, dict):
        return value
    raise ValueError("reply contains no JSON object")


async def _llm_call(messages: List[Dict], app, role: str, profile_id: str) -> str:
    """通用 LLM 调用（secondary，复用激活 preset）。"""
    config = app.state.config
    preset = config.get_analysis_preset() or config.get_active_llm_preset()
    if not preset:
        raise RuntimeError("No LLM preset configured")

    from src.llm.registry import get_provider
    llm = get_provider(preset)
    model = preset.get("model", "unknown")

    log_secondary_llm_call(role=role, messages=messages, model=model, gen_kwargs={}, session_id=profile_id)
    t0 = time.time()
    raw = ""
    exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            raw = ""
            async for token in llm.stream_chat(messages):
                raw += token
            exc = None
            break
        except Exception as e:
            exc = e
            if attempt == 0:
                logger.warning("[PersonaEvolution] LLM stream 失败，重试: %s", e)
                import asyncio
                await asyncio.sleep(3.0)
    if exc:
        raise exc

    log_secondary_llm_response(
        role=role, response=raw, model=model,
        session_id=profile_id,
        duration_ms=int((time.time() - t0) * 1000),
    )
    return raw


# ── 核心锚点提炼 ──────────────────────────────────────────────────────────────

async def extract_anchor(
    persona_name: str,
    base_prompt: str,
    style_constraint: str,
    app,
    profile_id: str = "",
) -> Optional[str]:
    """从 base_prompt + style_constraint 提炼 core_anchor 字符串。
    返回分号分隔的特质列表，失败时返回 None。
    """
    locale = get_locale()
    system_prompt = get_prompt("persona_evolution.anchor_extract", locale=locale)
    if not system_prompt:
        logger.error("[PersonaEvolution] anchor_extract prompt 未找到")
        return None

    system_text = Template(system_prompt).safe_substitute(
        persona_name=persona_name,
        base_prompt=base_prompt.strip(),
        style_constraint=style_constraint.strip(),
    )
    user_text = get_prompt("persona_evolution.anchor_extract_user", locale=locale) or \
        ("请根据以上人格设定，提炼核心锚点。只输出 JSON 对象，不要任何前缀或解释。"
         if locale == "zh" else
         "Based on the persona above, distill the core anchor. Output only the JSON object, no prefix or explanation.")

    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]

    try:
        raw = await _llm_call(messages, app, role="persona_anchor_extract", profile_id=profile_id)
        data = _parse_json_object(raw)
        anchor = (data.get("core_anchor") or "").strip()
        if anchor:
            logger.info("[PersonaEvolution] anchor 提炼完成 profile=%s anchor=%.80s", profile_id, anchor)
            return anchor
        logger.warning("[PersonaEvolution] anchor_extract 返回空 anchor，raw=%.200s", raw)
        return None
    except Exception as e:
        logger.error("[PersonaEvolution] anchor 提炼失败 profile=%s: %s\n%s",
                     profile_id, e, traceback.format_exc())
        log_error("persona_evolution", str(e), {"profile_id": profile_id, "op": "anchor_extract"},
                  traceback_str=traceback.format_exc())
        return None


# ── 人格演化触发 ──────────────────────────────────────────────────────────────

def _get_affinity_status(storage_root: str) -> str:
    """读取当前好感度阶段标签，无状态文件时返回默认。"""
    path = os.path.join(storage_root, "affinity_state.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            return state.get("status", "陌生")
    except Exception:
        pass
    return "陌生"


def _get_recent_facts_text(storage_root: str, max_facts: int = 50,
                           since_ts: float = 0.0) -> str:
    """从 LongTermStore 取上次演化以来的高权重事实，格式化为文本。

    Args:
        max_facts: 最大条数上限
        since_ts: 只取 updated_at >= since_ts 的事实（0 表示不限）
    """
    try:
        from src.memory.long_term_store import LongTermStore
        store = LongTermStore(storage_root)
        # Public API in LongTermStore is get_all(); keep a small compatibility fallback.
        if hasattr(store, "get_all"):
            facts = store.get_all()
        else:
            facts = getattr(store, "all_facts")()
        # 按 updated_at 降序，过滤权重和时间
        facts = sorted(facts, key=lambda f: getattr(f, "updated_at", 0), reverse=True)
        facts = [f for f in facts
                 if getattr(f, "weight", 0) > 0.3
                 and (since_ts <= 0 or getattr(f, "updated_at", 0) >= since_ts)]
        facts = facts[:max_facts]
        if not facts:
            return ""
        lines = []
        for f in facts:
            content = (getattr(f, "content", "") or "").strip()
            category = getattr(f, "category", "other") or "other"
            if content:
                lines.append(f"- [{category}] {content}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("[PersonaEvolution] 读取 facts 失败: %s", e)
        return ""


def _get_recent_conversation_text(storage_root: str, max_turns: int = 40) -> str:
    """从 ConversationStore 取最近 N 轮对话，格式化为文本。"""
    try:
        from src.memory.conversation_store import ConversationStore
        store = ConversationStore(storage_root, max_turns=0)
        turns = store.get_recent(max_turns)
        if not turns:
            return ""
        lines = []
        for turn in turns:
            role = turn.get("role", "")
            content = (turn.get("content") or "").strip()
            sender = turn.get("sender", "")
            if not content:
                continue
            if role == "user":
                label = sender if sender else "用户"
            else:
                label = sender if sender else "AI"
            lines.append(f"{label}: {content}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("[PersonaEvolution] 读取对话失败: %s", e)
        return ""


async def trigger_evolution(
    profile_id: str,
    storage_root: str,
    app,
    turn_counter: int = 0,
) -> Optional[Dict[str, Any]]:
    """执行一次人格演化重构。

    - 读取当前 persona_evolved（或原始 base_prompt/style_constraint）
    - 确保 core_anchor 存在（懒生成）
    - 调用 LLM 重构
    - 写入 profile.json["persona_evolved"] 和 persona_changelog.json
    返回写入的 changelog entry，失败或跳过返回 None。
    """
    card = _load_profile_card(profile_id)
    if not card:
        logger.warning("[PersonaEvolution] profile 不存在，跳过演化: %s", profile_id)
        return None

    base_prompt_original = (card.get("base_prompt") or "").strip()
    style_constraint_original = (card.get("style_constraint") or "").strip()
    persona_name = card.get("display_name") or card.get("name") or profile_id

    if not base_prompt_original:
        logger.info("[PersonaEvolution] base_prompt 为空，跳过演化 profile=%s", profile_id)
        return None

    evolved = card.get("persona_evolved") or {}

    # ── 检查演化开关 ──────────────────────────────────────────────────────────
    if not evolved.get("enabled", True):
        logger.debug("[PersonaEvolution] 演化已关闭，跳过 profile=%s", profile_id)
        return None

    # ── 确保 core_anchor 存在（懒生成）──────────────────────────────────────
    core_anchor = (evolved.get("core_anchor") or "").strip()
    if not core_anchor:
        logger.info("[PersonaEvolution] core_anchor 不存在，先提炼 profile=%s", profile_id)
        core_anchor = await extract_anchor(
            persona_name, base_prompt_original, style_constraint_original,
            app, profile_id=profile_id,
        ) or ""
        if not core_anchor:
            logger.warning("[PersonaEvolution] anchor 提炼失败，跳过本次演化 profile=%s", profile_id)
            return None
        # Save only the anchor this round; the full evolution runs on the next trigger
        if _update_persona_evolved(profile_id, {"core_anchor": core_anchor}):
            logger.info("[PersonaEvolution] core_anchor 初始化完成，跳过本轮演化（下次触发时再重构）profile=%s", profile_id)
        return None

    # ── 准备重构输入 ─────────────────────────────────────────────────────────
    current_base = (evolved.get("base_prompt") or base_prompt_original).strip()
    current_style = (evolved.get("style_constraint") or style_constraint_original).strip()
    evolution_count = int(evolved.get("evolution_count", 0))
    baseline_review_interval = int(
        evolved.get("baseline_review_interval", DEFAULT_BASELINE_REVIEW_INTERVAL) or 0
    )
    baseline_review = _is_baseline_review(evolution_count, baseline_review_interval)

    max_facts = int(evolved.get("max_evolution_facts", 50))
    max_conv_turns = int(evolved.get("max_evolution_conv_turns", 40))
    since_ts = float(evolved.get("evolved_at", 0))

    memory_facts = _get_recent_facts_text(storage_root, max_facts=max_facts, since_ts=since_ts)
    if not memory_facts:
        logger.info("[PersonaEvolution] 无足够记忆事实，跳过演化 profile=%s", profile_id)
        return None

    conversation_text = _get_recent_conversation_text(storage_root, max_turns=max_conv_turns)
    affinity_status = _get_affinity_status(storage_root)

    locale = get_locale()
    from src.core.user_portrait import (
        extract_style_address_form,
        format_mutual_address_facts,
        _resolve_user_intro,
    )
    explicit_name, _ = _resolve_user_intro(card, app)
    mutual_address = format_mutual_address_facts(
        explicit_name,
        card.get("user_portrait") or {},
        locale=locale,
        style_address_form="" if explicit_name else extract_style_address_form(card),
    )

    system_prompt = get_prompt("persona_evolution.evolve", locale=locale)
    if not system_prompt:
        logger.error("[PersonaEvolution] evolve prompt 未找到")
        return None

    # 长度自适应：软上限按原始 base_prompt 长度动态算（尊重原作量级），不少于 1500
    soft_cap = max(1500, int(len(base_prompt_original) * 1.5))
    hard_cap = int(soft_cap * 1.15)
    current_length = len(current_base)

    system_text = Template(system_prompt).safe_substitute(
        persona_name=persona_name,
        core_anchor=core_anchor,
        original_base_prompt=base_prompt_original,
        original_style_constraint=style_constraint_original,
        current_base_prompt=current_base,
        current_style_constraint=current_style,
        memory_facts=memory_facts,
        conversation_context=conversation_text or ("（无近期对话）" if locale == "zh" else "(no recent conversation)"),
        mutual_address=mutual_address or (
            "（双方称呼尚未确认）" if locale == "zh" else "(mutual address forms not yet confirmed)"
        ),
        affinity_status=affinity_status,
        evolution_count=str(evolution_count),
        rewrite_mode=(
            "基线复核：以原始人格为骨架，只保留有长期证据的既有变化"
            if locale == "zh" and baseline_review else
            "常规微调：以原始人格为边界，审慎评估当前版本中的候选变化"
            if locale == "zh" else
            "Baseline review: rebuild on the original persona and retain only previously learned changes with durable evidence"
            if baseline_review else
            "Regular adjustment: treat the original persona as the boundary and evaluate current changes cautiously"
        ),
        current_length=str(current_length),
        soft_cap=str(soft_cap),
    )
    user_text = get_prompt("persona_evolution.evolve_user", locale=locale) or \
        ("请根据以上记忆依据，对当前人格做出微小修正。只输出 JSON 对象，不要任何前缀或解释。"
         if locale == "zh" else
         "Based on the memory evidence above, make small revisions to the current persona. Output only the JSON object, no prefix or explanation.")

    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]

    # ── LLM 调用 ─────────────────────────────────────────────────────────────
    try:
        raw = await _llm_call(messages, app, role="persona_evolve", profile_id=profile_id)
        data = _parse_json_object(raw)
    except Exception as e:
        logger.error("[PersonaEvolution] evolve LLM 调用或解析失败 profile=%s: %s", profile_id, e)
        log_error("persona_evolution", str(e), {"profile_id": profile_id, "op": "evolve"},
                  traceback_str=traceback.format_exc())
        return None

    # ── 验证 anchor_preserved ────────────────────────────────────────────────
    if not data.get("anchor_preserved", True) and current_length >= soft_cap:
        # Over the soft cap, compression alone is a valid result. Skipping would leave an over-long
        # persona untouched for good, since most rounds bring no durable new evidence.
        followup = (
            f"当前 base_prompt 为 {current_length} 字，超过软上限 {soft_cap} 字，本轮不能跳过。"
            f"即使没有需要改动的长期证据，也请只做压缩：保留全部章节与核心锚点，合并重复描述，"
            f"删去不受原始人格或长期证据支持的累积内容，使 base_prompt ≤ {soft_cap} 字，"
            f"anchor_preserved 填 true。仅输出 JSON 对象，不要任何前缀或解释。"
            if locale == "zh" else
            f"The current base_prompt is {current_length} chars, over the soft cap of {soft_cap}, so this "
            f"round cannot be skipped. Even without durable evidence for changes, compress only: keep every "
            f"section and the core anchor, merge redundant descriptions, and remove accumulated content not "
            f"supported by the original persona or durable evidence, so base_prompt ≤ {soft_cap} chars. "
            f"Set anchor_preserved to true. Output only the JSON object, no prefix or explanation."
        )
        try:
            raw = await _llm_call(
                messages + [{"role": "assistant", "content": raw}, {"role": "user", "content": followup}],
                app, role="persona_evolve_compress", profile_id=profile_id,
            )
            data = _parse_json_object(raw)
        except Exception as e:
            logger.warning("[PersonaEvolution] compression-only retry failed profile=%s: %s", profile_id, e)
            return None

    if not data.get("anchor_preserved", True):
        reason = data.get("reason", "（无说明）")
        logger.info("[PersonaEvolution] LLM 判断无法在保留 anchor 前提下演化，跳过 profile=%s reason=%s",
                    profile_id, reason)
        return None

    new_base = (data.get("base_prompt") or "").strip()
    new_style = (data.get("style_constraint") or "").strip()
    # change_summary 可能是 string 或 list（prompt 要求列点摘要时 LLM 常返回数组）
    raw_summary = data.get("change_summary") or ""
    if isinstance(raw_summary, list):
        change_summary = "\n".join(
            f"- {str(item).strip().lstrip('-').strip()}"
            for item in raw_summary if str(item).strip()
        )
    else:
        change_summary = str(raw_summary).strip()
    reason = (data.get("reason") or "").strip()

    if not new_base:
        logger.warning("[PersonaEvolution] LLM 返回 base_prompt 为空，跳过 profile=%s", profile_id)
        return None

    # ── 硬兜底：超过 hard_cap 重试一次强化压缩；仍超则跳过本次演化 ──────────────
    if len(new_base) > hard_cap:
        followup = (
            f"新版 base_prompt 当前 {len(new_base)} 字，超过硬上限 {hard_cap} 字。"
            f"请重新输出：保留本次最多 0～2 处稳定改动，同时更激进地压缩冗余/过时段落，"
            f"使 base_prompt 总长 ≤ {soft_cap} 字。仅输出 JSON 对象，不要任何前缀或解释。"
            if locale == "zh" else
            f"The new base_prompt is currently {len(new_base)} chars, exceeding the hard cap of "
            f"{hard_cap}. Re-output: keep at most 0–2 stable changes but compress redundant/stale "
            f"passages more aggressively so base_prompt ≤ {soft_cap} chars. Output only the JSON, "
            f"no prefix or explanation."
        )
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": followup},
        ]
        try:
            raw = await _llm_call(retry_messages, app, role="persona_evolve_retry", profile_id=profile_id)
            data = _parse_json_object(raw)
            new_base = (data.get("base_prompt") or "").strip()
            new_style = (data.get("style_constraint") or new_style).strip()
            raw_summary = data.get("change_summary") or change_summary
            if isinstance(raw_summary, list):
                change_summary = "\n".join(
                    f"- {str(item).strip().lstrip('-').strip()}"
                    for item in raw_summary if str(item).strip()
                )
            else:
                change_summary = str(raw_summary).strip()
            reason = (data.get("reason") or reason).strip()
        except Exception as e:
            logger.warning("[PersonaEvolution] 压缩重试失败 profile=%s: %s", profile_id, e)
            return None
        if not new_base or len(new_base) > hard_cap:
            logger.info("[PersonaEvolution] 重试后仍超长(%d > %d)或为空，跳过本次演化 profile=%s",
                        len(new_base), hard_cap, profile_id)
            return None
        logger.info("[PersonaEvolution] 压缩重试成功 profile=%s 新长度=%d (soft=%d hard=%d)",
                    profile_id, len(new_base), soft_cap, hard_cap)

    valid, new_style, validation_reason = _validate_evolution_output(
        base_prompt_original,
        new_base,
        current_style,
        new_style,
    )
    if not valid:
        logger.info(
            "[PersonaEvolution] 新版未通过基线验证，跳过 profile=%s reason=%s",
            profile_id,
            validation_reason,
        )
        return None

    if new_base == current_base and new_style == current_style:
        logger.info("[PersonaEvolution] evolution produced no change, nothing saved profile=%s", profile_id)
        return None

    # ── 写入 profile.json["persona_evolved"] ─────────────────────────────────
    now_ts = int(time.time())
    new_evolution_count = evolution_count + 1

    saved = _update_persona_evolved(profile_id, {
        "base_prompt": new_base,
        "style_constraint": new_style,
        "core_anchor": core_anchor,
        "evolved_at": now_ts,
        "evolution_count": new_evolution_count,
    })
    if not saved:
        logger.info("[PersonaEvolution] profile removed or evolution disabled during the model call, "
                    "nothing saved profile=%s", profile_id)
        return None

    # ── 写入 changelog ────────────────────────────────────────────────────────
    entry = {
        "version": new_evolution_count,
        "evolved_at": now_ts,
        "change_summary": change_summary,
        "reason": reason,
        "baseline_review": baseline_review,
        "base_prompt_before": current_base,
        "style_constraint_before": current_style,
        "base_prompt_after": new_base,
        "style_constraint_after": new_style,
    }
    _append_changelog(storage_root, entry)

    logger.info("[PersonaEvolution] 演化完成 profile=%s version=%d summary=%s",
                profile_id, new_evolution_count, change_summary)
    return entry


# ── 回滚 ─────────────────────────────────────────────────────────────────────

def rollback(profile_id: str, storage_root: str, version: int) -> bool:
    """将 persona_evolved 回滚到指定 version。保留 core_anchor 和 evolution_count 不变。"""
    changelog = get_changelog(storage_root)
    entry = next((e for e in changelog if e.get("version") == version), None)
    if not entry:
        logger.warning("[PersonaEvolution] rollback 找不到 version=%d profile=%s", version, profile_id)
        return False

    card = _load_profile_card(profile_id)
    if not card:
        return False

    evolved = card.get("persona_evolved") or {}
    evolved["base_prompt"] = entry["base_prompt_before"]
    evolved["style_constraint"] = entry["style_constraint_before"]
    # core_anchor 和 evolution_count 保持不变
    card["persona_evolved"] = evolved
    _save_profile_card(profile_id, card)

    logger.info("[PersonaEvolution] 回滚完成 profile=%s -> version %d (before)", profile_id, version)
    return True
