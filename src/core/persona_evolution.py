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
    path = _profile_card_path(profile_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)


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

def _strip_fence(raw: str) -> str:
    """去除 LLM 输出中可能包裹的 markdown 代码块。"""
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    return m.group(1).strip() if m else raw.strip()


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
        data = json.loads(_strip_fence(raw))
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
        facts = store.all_facts()
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
        # 写入 core_anchor 但先不做完整演化（本轮仅建立锚点）
        evolved["core_anchor"] = core_anchor
        card["persona_evolved"] = evolved
        _save_profile_card(profile_id, card)
        logger.info("[PersonaEvolution] core_anchor 初始化完成，跳过本轮演化（下次触发时再重构）profile=%s", profile_id)
        return None

    # ── 准备重构输入 ─────────────────────────────────────────────────────────
    current_base = (evolved.get("base_prompt") or base_prompt_original).strip()
    current_style = (evolved.get("style_constraint") or style_constraint_original).strip()
    evolution_count = int(evolved.get("evolution_count", 0))

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
    system_prompt = get_prompt("persona_evolution.evolve", locale=locale)
    if not system_prompt:
        logger.error("[PersonaEvolution] evolve prompt 未找到")
        return None

    system_text = Template(system_prompt).safe_substitute(
        persona_name=persona_name,
        core_anchor=core_anchor,
        current_base_prompt=current_base,
        current_style_constraint=current_style,
        memory_facts=memory_facts,
        conversation_context=conversation_text or ("（无近期对话）" if locale == "zh" else "(no recent conversation)"),
        affinity_status=affinity_status,
        evolution_count=str(evolution_count),
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
        data = json.loads(_strip_fence(raw))
    except Exception as e:
        logger.error("[PersonaEvolution] evolve LLM 调用或解析失败 profile=%s: %s", profile_id, e)
        log_error("persona_evolution", str(e), {"profile_id": profile_id, "op": "evolve"},
                  traceback_str=traceback.format_exc())
        return None

    # ── 验证 anchor_preserved ────────────────────────────────────────────────
    if not data.get("anchor_preserved", True):
        reason = data.get("reason", "（无说明）")
        logger.info("[PersonaEvolution] LLM 判断无法在保留 anchor 前提下演化，跳过 profile=%s reason=%s",
                    profile_id, reason)
        return None

    new_base = (data.get("base_prompt") or "").strip()
    new_style = (data.get("style_constraint") or "").strip()
    change_summary = (data.get("change_summary") or "").strip()
    reason = (data.get("reason") or "").strip()

    if not new_base:
        logger.warning("[PersonaEvolution] LLM 返回 base_prompt 为空，跳过 profile=%s", profile_id)
        return None

    # ── 写入 profile.json["persona_evolved"] ─────────────────────────────────
    now_ts = int(time.time())
    new_evolution_count = evolution_count + 1

    card.setdefault("persona_evolved", {})
    card["persona_evolved"].update({
        "base_prompt": new_base,
        "style_constraint": new_style,
        "core_anchor": core_anchor,
        "evolved_at": now_ts,
        "evolution_count": new_evolution_count,
    })
    _save_profile_card(profile_id, card)

    # ── 写入 changelog ────────────────────────────────────────────────────────
    entry = {
        "version": new_evolution_count,
        "evolved_at": now_ts,
        "change_summary": change_summary,
        "reason": reason,
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
