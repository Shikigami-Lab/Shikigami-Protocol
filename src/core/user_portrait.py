"""user_portrait.py — AI 维护的用户画像引擎

职责：
  - trigger_portrait_refresh : 跑一次画像刷新（自动 / 手动）
  - get_changelog            : 读取画像历史
  - rollback                 : 回滚到指定版本

数据存储：
  - profiles/<id>.json["user_portrait"]               — 当前画像内容（content / updated_at / refresh_count / last_input_signature）
  - profiles/<id>.json["user_portrait_config"]        — 画像配置（开关 / 频率 / 输入策略）
  - profiles/<id>/user_portrait_changelog.json        — 画像历史（最近 MAX_CHANGELOG_ENTRIES 条）

设计原则：
  - 完全独立于 memory_config 总开关；day_summary / facts 都视为"加分项"，没有也能跑
  - 输入特征 hash 短路：输入完全没变就不跑 LLM
  - 必须带反污染条款，禁止把对话里的少见术语刻进画像
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import traceback
from string import Template
from typing import Any, Dict, List, Optional, Tuple

from src.config.prompt_loader import get_prompt, get_locale
from src.utils.debug_logger import log_secondary_llm_call, log_secondary_llm_response, log_error
from src.utils.paths import get_project_root

logger = logging.getLogger(__name__)

_PROFILES_DIR = os.path.join(get_project_root(), "profiles")
_CHANGELOG_FILE = "user_portrait_changelog.json"
MAX_CHANGELOG_ENTRIES = 20


# ── 路径与 I/O ────────────────────────────────────────────────────────────────

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
        logger.warning("[UserPortrait] 读取 profile card 失败 %s: %s", profile_id, e)
        return {}


def _save_profile_card(profile_id: str, card: Dict[str, Any]) -> None:
    # 原子写：人格卡是角色的核心文件，崩溃在写入中途会损坏整个角色
    path = _profile_card_path(profile_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_changelog(storage_root: str) -> List[Dict[str, Any]]:
    """返回画像历史列表（最新在前）。"""
    path = _changelog_path(storage_root)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("[UserPortrait] 读取 changelog 失败: %s", e)
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
    """只剥"整段被 ``` 包裹"的外层 fence，绝不剥中间嵌入的代码块。

    portrait 输出是 markdown 正文，LLM 可能在中间塞代码块示例。如果用
    `re.search(r'```...```')` 非贪婪匹配，会只截到第一对 ``` 之间的内容，
    后面正文全部丢失——这是之前画像"在某处被截断"的根因。
    """
    s = raw.strip()
    m = re.match(r"^```(?:json|markdown|md)?[ \t]*\n?([\s\S]*?)\n?```\s*$", s)
    if m:
        return m.group(1).strip()
    return s


async def _llm_call(messages: List[Dict], app, role: str, profile_id: str) -> str:
    """画像调用使用与记忆提取相同的 preset（profile.memory_config.extraction_llm_preset）。

    解析规则与 MemoryManager 一致：
      "__none__"  → 不能用，fall back 到激活 preset（画像不应被禁）
      ""           → 激活 preset（主对话模型）
      "<name>"    → 指定 preset；不存在时 fall back 激活 preset
    """
    config = app.state.config
    try:
        _card = _load_profile_card(profile_id)
        _mem_cfg = _card.get("memory_config") or {}
    except Exception:
        _mem_cfg = {}
    extraction_preset_name = (_mem_cfg.get("extraction_llm_preset") or "").strip()
    if extraction_preset_name and extraction_preset_name != "__none__":
        preset = config.get_llm_preset(extraction_preset_name)
    else:
        preset = config.get_active_llm_preset()
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
                logger.warning("[UserPortrait] LLM stream 失败，重试: %s", e)
                await asyncio.sleep(3.0)
    if exc:
        raise exc

    log_secondary_llm_response(
        role=role, response=raw, model=model,
        session_id=profile_id,
        duration_ms=int((time.time() - t0) * 1000),
    )
    return raw


# ── 输入收集 ──────────────────────────────────────────────────────────────────

def _resolve_user_intro(card: Dict[str, Any], app) -> Tuple[str, str]:
    """合并全局 + profile 的 user_persona，返回 (name, introduction)。profile 优先。"""
    global_persona = getattr(getattr(app, "state", None), "config", None)
    g_up = getattr(global_persona, "user_persona", {}) or {} if global_persona else {}
    p_up = card.get("user_persona") or {}
    # 兼容老 schema：profile 侧若仍是 description/personality，拼成 introduction
    p_intro = (p_up.get("introduction") or "").strip()
    if not p_intro:
        legacy_parts = [
            (p_up.get("description") or "").strip(),
            (p_up.get("personality") or "").strip(),
            (p_up.get("role_in_story") or "").strip(),
        ]
        p_intro = "\n\n".join([s for s in legacy_parts if s])
    g_intro = (g_up.get("introduction") or "").strip()
    if not g_intro:
        legacy_parts = [
            (g_up.get("description") or "").strip(),
            (g_up.get("personality") or "").strip(),
        ]
        g_intro = "\n\n".join([s for s in legacy_parts if s])
    name = (p_up.get("name") or g_up.get("name") or "").strip()
    introduction = p_intro or g_intro
    return name, introduction


def _get_recent_conversation_text(storage_root: str, max_turns: int) -> str:
    """从 ConversationStore 取最近 max_turns 轮对话。"""
    if max_turns <= 0:
        return ""
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
            label = sender if sender else ("用户" if role == "user" else "AI")
            lines.append(f"{label}: {content}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("[UserPortrait] 读取对话失败: %s", e)
        return ""


def _get_recent_day_summaries_text(storage_root: str, n_days: int = 7) -> str:
    """从 DaySummaryStore 取最近 n_days 天的日记摘要。"""
    try:
        from src.memory.day_store import DaySummaryStore
        store = DaySummaryStore(storage_root)
        recent = store.get_recent(n_days)
        if not recent:
            return ""
        lines = []
        for entry in recent:
            d = entry.get("date", "")
            content = (entry.get("content") or entry.get("summary") or "").strip()
            if content:
                lines.append(f"[{d}] {content}")
        return "\n\n".join(lines)
    except Exception as e:
        logger.warning("[UserPortrait] 读取 day_summary 失败: %s", e)
        return ""


def _get_facts_text(storage_root: str, max_facts: int = 30) -> str:
    """从 LongTermStore 取高权重 facts。"""
    try:
        from src.memory.long_term_store import LongTermStore
        store = LongTermStore(storage_root)
        facts = store.get_all() if hasattr(store, "get_all") else getattr(store, "all_facts")()
        facts = sorted(facts, key=lambda f: getattr(f, "weight", 0), reverse=True)
        facts = [f for f in facts if getattr(f, "weight", 0) > 0.3][:max_facts]
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
        logger.warning("[UserPortrait] 读取 facts 失败: %s", e)
        return ""


def _compute_input_signature(
    current_portrait: str,
    introduction: str,
    conversation: str,
    day_summaries: str,
    facts: str,
) -> str:
    """对所有输入计算 sha1，用于短路：输入完全没变就跳过 LLM 调用。"""
    h = hashlib.sha1()
    for piece in (current_portrait, introduction, conversation, day_summaries, facts):
        h.update((piece or "").encode("utf-8", errors="ignore"))
        h.update(b"\x00")
    return h.hexdigest()


# ── 冷却检查 ──────────────────────────────────────────────────────────────────

def _is_in_cooldown(portrait: Dict[str, Any], min_hours: float) -> bool:
    if min_hours <= 0:
        return False
    last_ts = float(portrait.get("updated_at") or 0)
    if last_ts <= 0:
        return False
    elapsed_h = (time.time() - last_ts) / 3600
    return elapsed_h < min_hours


# ── 主入口：刷新 ──────────────────────────────────────────────────────────────

async def trigger_portrait_refresh(
    profile_id: str,
    storage_root: str,
    app,
    *,
    force: bool = False,
    triggered_by: str = "auto",
) -> Optional[Dict[str, Any]]:
    """跑一次画像刷新。

    Args:
        force: 跳过冷却与 signature 短路（手动触发时用）
        triggered_by: 记到 changelog 的触发来源标签（daily / burst / manual / ...）
    返回 changelog entry，跳过或失败返回 None。
    """
    card = _load_profile_card(profile_id)
    if not card:
        logger.warning("[UserPortrait] profile 不存在，跳过 %s", profile_id)
        return None

    cfg = card.get("user_portrait_config") or {}
    if not cfg.get("enabled", True):
        logger.debug("[UserPortrait] 画像功能未启用，跳过 profile=%s", profile_id)
        return None

    portrait = card.get("user_portrait") or {}
    min_hours = float(cfg.get("min_hours_between_refresh", 6))
    if not force and _is_in_cooldown(portrait, min_hours):
        logger.debug("[UserPortrait] 仍在冷却期内（< %.1fh），跳过 profile=%s", min_hours, profile_id)
        return None

    # ── 收集输入 ─────────────────────────────────────────────────────────────
    name, introduction = _resolve_user_intro(card, app)
    max_conv_turns = int(cfg.get("max_input_conv_turns", 60))
    conversation = _get_recent_conversation_text(storage_root, max_conv_turns)
    day_summaries = _get_recent_day_summaries_text(storage_root, 7) if cfg.get("use_day_summary_as_input", True) else ""
    facts = _get_facts_text(storage_root, 30) if cfg.get("use_facts_as_input", True) else ""
    current_portrait = (portrait.get("content") or "").strip()

    if not conversation and not facts and not day_summaries and not current_portrait:
        logger.info("[UserPortrait] 无任何输入，跳过 profile=%s", profile_id)
        return None

    # signature 短路
    sig = _compute_input_signature(current_portrait, introduction, conversation, day_summaries, facts)
    if not force and sig == (portrait.get("last_input_signature") or ""):
        logger.debug("[UserPortrait] 输入特征未变，跳过 profile=%s", profile_id)
        return None

    # ── 取 LLM prompt ───────────────────────────────────────────────────────
    locale = get_locale()
    system_prompt = get_prompt("user_portrait.refresh", locale=locale)
    if not system_prompt:
        logger.error("[UserPortrait] user_portrait.refresh prompt 未找到")
        return None

    persona_name = card.get("display_name") or card.get("name") or profile_id
    user_name = (name or "").strip()  # 留空交给 prompt 占位处理

    # 角色 base_prompt（演化版优先），完整注入不截断
    evolved = card.get("persona_evolved") or {}
    if evolved.get("enabled", True):
        base_prompt_text = (evolved.get("base_prompt") or "").strip() or (card.get("base_prompt") or "").strip()
    else:
        base_prompt_text = (card.get("base_prompt") or "").strip()

    def _placeholder(s: str, zh: str, en: str) -> str:
        return s.strip() if s else (zh if locale == "zh" else en)

    system_text = Template(system_prompt).safe_substitute(
        persona_name=persona_name,
        base_prompt=_placeholder(base_prompt_text, "（暂无角色设定可参考）", "(no persona setting available)"),
        user_name=_placeholder(user_name, "（用户尚未设置称呼）", "(no preferred name set)"),
        introduction=_placeholder(introduction, "（用户尚未填写自我介绍）", "(no self-introduction provided)"),
        current_portrait=_placeholder(current_portrait, "（这是首次生成画像，旧画像为空）", "(no previous portrait — this is the first run)"),
        recent_conversation=_placeholder(conversation, "（无近期对话）", "(no recent conversation)"),
        day_summaries=_placeholder(day_summaries, "（无可用 day_summary）", "(no day_summary available)"),
        memory_facts=_placeholder(facts, "（无可用记忆事实）", "(no memory facts available)"),
        refresh_count=str(int(portrait.get("refresh_count") or 0)),
    )

    user_text = get_prompt("user_portrait.refresh_user", locale=locale) or (
        "请根据以上信息更新对方的画像。只输出新版画像 markdown 正文本身，不要任何前缀或解释。"
        if locale == "zh" else
        "Update the portrait based on the information above. Output only the new portrait markdown body, no prefix or explanation."
    )

    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]

    # ── LLM 调用 ─────────────────────────────────────────────────────────────
    try:
        raw = await _llm_call(messages, app, role="user_portrait_refresh", profile_id=profile_id)
    except Exception as e:
        logger.error("[UserPortrait] LLM 调用失败 profile=%s: %s", profile_id, e)
        log_error("user_portrait", str(e), {"profile_id": profile_id, "op": "refresh"},
                  traceback_str=traceback.format_exc())
        return None

    new_content = _strip_fence(raw).strip()
    if not new_content:
        logger.warning("[UserPortrait] LLM 返回空内容，跳过写入 profile=%s", profile_id)
        return None

    # ── 写入 ────────────────────────────────────────────────────────────────
    now_ts = int(time.time())
    new_refresh_count = int(portrait.get("refresh_count") or 0) + 1

    card.setdefault("user_portrait", {})
    card["user_portrait"].update({
        "content": new_content,
        "updated_at": now_ts,
        "refresh_count": new_refresh_count,
        "last_input_signature": sig,
    })
    _save_profile_card(profile_id, card)

    entry = {
        "version": new_refresh_count,
        "updated_at": now_ts,
        "triggered_by": triggered_by,
        "content_before": current_portrait,
        "content_after": new_content,
    }
    _append_changelog(storage_root, entry)

    logger.info("[UserPortrait] 画像刷新完成 profile=%s version=%d trigger=%s",
                profile_id, new_refresh_count, triggered_by)
    return entry


# ── 回滚 ─────────────────────────────────────────────────────────────────────

def rollback(profile_id: str, storage_root: str, version: int) -> bool:
    """将 user_portrait.content 回滚到指定 version 的 content_before。refresh_count 保持不变。"""
    changelog = get_changelog(storage_root)
    entry = next((e for e in changelog if e.get("version") == version), None)
    if not entry:
        logger.warning("[UserPortrait] rollback 找不到 version=%d profile=%s", version, profile_id)
        return False

    card = _load_profile_card(profile_id)
    if not card:
        return False

    portrait = card.get("user_portrait") or {}
    portrait["content"] = entry.get("content_before", "")
    portrait["last_input_signature"] = ""  # 重置 signature 让下次刷新会真跑
    card["user_portrait"] = portrait
    _save_profile_card(profile_id, card)

    logger.info("[UserPortrait] 回滚完成 profile=%s -> version %d (before)", profile_id, version)
    return True


# ── 手动编辑/清空 ────────────────────────────────────────────────────────────

def manual_edit(profile_id: str, new_content: str) -> bool:
    """用户手动编辑保存（不走 LLM）。保留 refresh_count，更新 updated_at，重置 signature。"""
    card = _load_profile_card(profile_id)
    if not card:
        return False
    card.setdefault("user_portrait", {})
    card["user_portrait"]["content"] = (new_content or "").strip()
    card["user_portrait"]["updated_at"] = int(time.time())
    card["user_portrait"]["last_input_signature"] = ""
    _save_profile_card(profile_id, card)
    return True


def reset_portrait(profile_id: str) -> bool:
    """清空 content（下次刷新从 introduction 重新长出）。refresh_count 保持不变。"""
    return manual_edit(profile_id, "")
