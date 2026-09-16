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
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Tuple

from src.config.profile_loader import DEFAULT_USER_PORTRAIT_CONFIG
from src.config.prompt_loader import get_prompt, get_locale
from src.utils.debug_logger import log_secondary_llm_call, log_secondary_llm_response, log_error
from src.utils.paths import get_project_root

logger = logging.getLogger(__name__)

_PROFILES_DIR = os.path.join(get_project_root(), "profiles")
_CHANGELOG_FILE = "user_portrait_changelog.json"
MAX_CHANGELOG_ENTRIES = 20


# ── Mutual address ───────────────────────────────────────────────────────────
#
# The user's name comes only from user_persona.name. A character card's style rules
# may say how the character addresses the user (称呼用户为「投喂者」 / call the user
# "feeder"). That is a form of address, not a name: it is kept apart from the name and
# only ever phrased as "you usually address them as X". Extraction is deliberately
# conservative and yields nothing for pronouns, placeholders, negated or conditional
# rules, passive or self-referential phrasing, rules about how others address the
# character, and conflicting rules.

# Generic person words are never a name or a form of address.
_GENERIC_PERSON_WORDS = frozenset({
    "你", "您", "你们", "您们", "他", "她", "它", "他们", "她们", "它们", "ta",
    "对方", "用户", "使用者",
    "you", "ya", "ye", "your", "they", "them", "he", "him", "she", "her", "it",
    "user", "the user",
})
# Values that describe a slot ("their name", "{{user}}") rather than a form of address.
_ADDRESS_PLACEHOLDER_RE = re.compile(
    r"名字|名称|昵称|用户名|真名|本名|\b(?:user|nick)?name\b|[{}<>]",
    re.IGNORECASE,
)
_QUOTED_FORM = r"""\s*[「『“"'‘](?P<form>[^」』”"'’\r\n]{1,40})[」』”"'’]"""
_ZH_USER_OBJECT = r"(?:用户|对方|使用者|主人|他|她|ta|TA|Ta)"


class _AddressRuleSyntax(NamedTuple):
    pattern: "re.Pattern[str]"
    clause_breaks: str                    # characters that end the clause a rule belongs to
    suffix_breaks: str                    # characters that end the text right after the quoted form
    negation_before: "re.Pattern[str]"    # searched in the clause text before the rule
    negation_after: "re.Pattern[str]"     # searched in the text right after the quoted form
    condition: "re.Pattern[str]"          # conditional rules ("after they get close") are skipped
    other_speaker: "re.Pattern[str]"      # passive, self-referential, or someone else speaking


_ZH_NEGATION_BEFORE_RE = re.compile(
    r"禁止|严禁|不要|不许|不能|不准|不可|不得|不应|不该|不会|不再|避免|切勿|勿|绝不|从不|拒绝"
    r"|(?<![特分区告识性级类个派辨鉴差])别(?!人)"
)
_ZH_NEGATION_AFTER_RE = re.compile(r"禁止|不允许|不可以|不行|不要|不准|不许|不能|不合适")
_ZH_CONDITION_RE = re.compile(r"(?<!平)时|后|如果|若|除非|只有|只在|直到|场合|情况下|(?<![适相应])当(?!然)")
_ZH_OTHER_SPEAKER_RE = re.compile(
    r"被|让人|让别人|自称|别人|他人|大家|人们"
    r"|(?:用户|对方|主人)(?:会|常|常常|总|总是|都|一般|通常|喜欢|习惯|经常)?$"
)
_EN_NEGATION_RE = re.compile(
    r"\b(?:never|not|no|avoid|stop|refrain|forbidden|prohibited)\b|n['’]t\b",
    re.IGNORECASE,
)
_EN_CONDITION_RE = re.compile(r"\b(?:when|if|once|after|unless|only|until|while)\b", re.IGNORECASE)
_EN_OTHER_SPEAKER_RE = re.compile(
    r"\b(?:users?|people|others|everyone|they|fans|someone)\s*$|\b(?:called|known as)\b",
    re.IGNORECASE,
)
_ZH_CLAUSE_BREAKS = "。！？!?；;\n"
_ZH_SUFFIX_BREAKS = "，,。！？!?；;\n"

_ADDRESS_RULE_SYNTAXES = (
    # 称呼用户为「X」 / 叫他做「X」
    _AddressRuleSyntax(
        re.compile(r"(?:称呼|称|叫|喊)" + _ZH_USER_OBJECT + r"(?:为|做|作)" + _QUOTED_FORM),
        _ZH_CLAUSE_BREAKS, _ZH_SUFFIX_BREAKS,
        _ZH_NEGATION_BEFORE_RE, _ZH_NEGATION_AFTER_RE, _ZH_CONDITION_RE, _ZH_OTHER_SPEAKER_RE,
    ),
    # 把用户称为「X」 / 将对方叫作「X」
    _AddressRuleSyntax(
        re.compile(r"(?:把|将)" + _ZH_USER_OBJECT + r"(?:称呼为|称为|称作|叫做|叫作)" + _QUOTED_FORM),
        _ZH_CLAUSE_BREAKS, _ZH_SUFFIX_BREAKS,
        _ZH_NEGATION_BEFORE_RE, _ZH_NEGATION_AFTER_RE, _ZH_CONDITION_RE, _ZH_OTHER_SPEAKER_RE,
    ),
    # call the user "X" / address them as "X"
    _AddressRuleSyntax(
        re.compile(
            r"\b(?:call|calls|address|addresses|refer to|refers to)\s+"
            r"(?:the user|the other person|your user|them|him|her)\s+(?:as\s+)?" + _QUOTED_FORM,
            re.IGNORECASE,
        ),
        ".!?;\n", ",.!?;\n",
        _EN_NEGATION_RE, _EN_NEGATION_RE, _EN_CONDITION_RE, _EN_OTHER_SPEAKER_RE,
    ),
)


def _effective_style_constraint(card: Dict[str, Any]) -> str:
    """Style rules as the chat prompt uses them: the evolved copy only while evolution is on."""
    root = (card.get("style_constraint") or "").strip()
    evolved = card.get("persona_evolved") or {}
    if evolved.get("enabled", True):
        return (evolved.get("style_constraint") or "").strip() or root
    return root


def _rule_context(text: str, start: int, end: int, syntax: _AddressRuleSyntax) -> Tuple[str, str]:
    """Return the clause text before a matched rule and the text right after its quoted form."""
    clause_start = max(text.rfind(ch, 0, start) for ch in syntax.clause_breaks) + 1
    suffix_end = min(
        (pos for pos in (text.find(ch, end) for ch in syntax.suffix_breaks) if pos >= 0),
        default=len(text),
    )
    return text[clause_start:start], text[end:suffix_end]


def _is_usable_address_form(form: str) -> bool:
    value = (form or "").strip()
    if not value or len(value) > MAX_ADDRESS_FORM_LEN:
        return False
    if value.casefold() in _GENERIC_PERSON_WORDS:
        return False
    return not _ADDRESS_PLACEHOLDER_RE.search(value)


def extract_style_address_form(card: Optional[Dict[str, Any]]) -> str:
    """Return the form of address the character's style rules use for the user, or "".

    The result is never treated as the user's name. It is non-empty only when the style
    rules contain exactly one distinct, affirmative, unconditional rule such as
    称呼用户为「X」 or call the user "X".
    """
    style = _effective_style_constraint(card or {})
    if not style:
        return ""
    forms: List[str] = []
    for syntax in _ADDRESS_RULE_SYNTAXES:
        for match in syntax.pattern.finditer(style):
            prefix, suffix = _rule_context(style, match.start(), match.end(), syntax)
            if (
                syntax.negation_before.search(prefix)
                or syntax.negation_after.search(suffix)
                or syntax.condition.search(prefix)
                or syntax.other_speaker.search(prefix)
            ):
                continue
            form = match.group("form").strip()
            if _is_usable_address_form(form) and all(form.casefold() != f.casefold() for f in forms):
                forms.append(form)
    return forms[0] if len(forms) == 1 else ""


def list_address_form_names(portrait_block: Optional[Dict[str, Any]], max_forms: int = 8) -> List[str]:
    """Return known user→character address forms, highest count first."""
    block = (portrait_block or {}).get("address_forms") or {}
    forms = [
        f for f in (block.get("forms") or [])
        if isinstance(f, dict) and str(f.get("form") or "").strip()
    ]
    names: List[str] = []
    seen = set()
    for item in forms:
        name = str(item.get("form") or "").strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
        if len(names) >= max_forms:
            break
    return names


def format_mutual_address_facts(
    user_name: str = "",
    portrait_block: Optional[Dict[str, Any]] = None,
    *,
    locale: str = "zh",
    max_forms: int = 3,
    style_address_form: str = "",
) -> str:
    """Plain mutual-address facts for prompts. No meta instructions.

    user_name is the explicit user_persona.name. style_address_form (from
    extract_style_address_form) is used only when no name is set, and is phrased as
    the form the character usually uses, never as a name.
    """
    name = (user_name or "").strip()
    style_form = "" if name else (style_address_form or "").strip()
    forms = list_address_form_names(portrait_block, max_forms=max_forms)
    lines: List[str] = []
    if locale == "en":
        if name:
            lines.append(f"You address them as \"{name}\".")
        elif style_form:
            lines.append(f"You usually address them as \"{style_form}\".")
        if len(forms) == 1:
            lines.append(f"They address you as \"{forms[0]}\".")
        elif len(forms) > 1:
            others = "\", \"".join(forms[1:])
            lines.append(f"They address you as \"{forms[0]}\", and sometimes \"{others}\".")
    else:
        if name:
            lines.append(f"你称呼对方为「{name}」。")
        elif style_form:
            lines.append(f"你通常称呼对方为「{style_form}」。")
        if len(forms) == 1:
            lines.append(f"对方称呼你为「{forms[0]}」。")
        elif len(forms) > 1:
            others = "」「".join(forms[1:])
            lines.append(f"对方称呼你为「{forms[0]}」，有时也叫「{others}」。")
    return "\n".join(lines)


# ── Lenient JSON reading for LLM replies ─────────────────────────────────────

_EMBEDDED_FENCE_RE = re.compile(r"```[A-Za-z]*[ \t]*\r?\n([\s\S]*)\r?\n[ \t]*```")


def _iter_json_values(raw: str, expected_type: type) -> Iterator[Any]:
    """Yield JSON values of expected_type (dict or list) found in an LLM reply, likeliest first.

    Replies are often not bare JSON: the payload may be wrapped in a code fence, have
    prose around the fence, or contain raw newlines inside long string values. Each
    candidate text (the reply without a whole-reply fence, the body of a fenced block
    surrounded by prose, then the reply itself) is decoded from its first opening
    bracket while ignoring trailing prose, and then as the span from the first opening
    to the last closing bracket. strict=False accepts control characters in strings.
    """
    text = (raw or "").strip()
    if not text:
        return
    opener, closer = ("[", "]") if expected_type is list else ("{", "}")
    decoder = json.JSONDecoder(strict=False)
    candidates = [_strip_fence(text)]
    embedded = _EMBEDDED_FENCE_RE.search(text)
    if embedded:
        candidates.append(embedded.group(1).strip())
    candidates.append(text)
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        start = candidate.find(opener)
        if start < 0:
            continue
        try:
            value = decoder.raw_decode(candidate, start)[0]
        except ValueError:
            value = None
        if isinstance(value, expected_type):
            yield value
        end = candidate.rfind(closer)
        if end > start:
            try:
                value = decoder.decode(candidate[start:end + 1])
            except ValueError:
                value = None
            if isinstance(value, expected_type):
                yield value


def _parse_portrait_payload(raw: str) -> Optional[Dict[str, Any]]:
    """Parse and normalize the three-layer portrait response.

    Returns None when no JSON object in the reply has a non-empty "content".
    """
    for data in _iter_json_values(raw, dict):
        parsed = _normalize_portrait_payload(data)
        if parsed:
            return parsed
    return None


def _normalize_portrait_payload(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Validate the required content field and normalize the three portrait layers."""
    content = str(data.get("content") or "").strip()
    if not content:
        return None

    recent_raw = data.get("recent_context") or ""
    if isinstance(recent_raw, dict):
        recent_context = str(recent_raw.get("content") or "").strip()
    else:
        recent_context = str(recent_raw).strip()

    details: List[Dict[str, Any]] = []
    seen_summaries = set()
    raw_details = data.get("contextual_details") or []
    for item in raw_details if isinstance(raw_details, list) else []:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()[:240]
        if not summary or summary.casefold() in seen_summaries:
            continue
        raw_keywords = item.get("keywords") or []
        if not isinstance(raw_keywords, list):
            continue
        keywords: List[str] = []
        seen_keywords = set()
        for raw_keyword in raw_keywords:
            keyword = str(raw_keyword or "").strip()[:40]
            key = keyword.casefold()
            if not keyword or key in seen_keywords:
                continue
            seen_keywords.add(key)
            keywords.append(keyword)
            if len(keywords) >= 6:
                break
        if len(keywords) < 2:
            continue
        seen_summaries.add(summary.casefold())
        details.append({"summary": summary, "keywords": keywords})
        if len(details) >= 5:
            break

    return {
        "content": content,
        "recent_context": recent_context,
        "contextual_details": details,
    }


# ── Portrait layers: recent context and contextual details ──────────────────

def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def recent_context_ttl_hours(cfg: Optional[Dict[str, Any]]) -> float:
    """TTL of the recent-context layer from user_portrait_config, falling back to the default."""
    default = float(DEFAULT_USER_PORTRAIT_CONFIG["recent_context_ttl_hours"])
    return _as_float((cfg or {}).get("recent_context_ttl_hours", default), default)


def recent_context_status(
    portrait_block: Optional[Dict[str, Any]],
    ttl_hours: Any,
    now_ts: float,
) -> Dict[str, Any]:
    """Describe the recent-context layer exactly as the chat prompt applies it.

    Returns content, updated_at, expires_at (0 when it never expires) and expired.
    Expired content stays stored but is no longer sent to the model. A legacy plain
    string never expires; with a TTL set, content without a timestamp counts as expired.
    """
    block = (portrait_block or {}).get("recent_context") or {}
    if isinstance(block, str):
        return {"content": block.strip(), "updated_at": 0, "expires_at": 0, "expired": False}
    if not isinstance(block, dict):
        return {"content": "", "updated_at": 0, "expires_at": 0, "expired": False}
    content = str(block.get("content") or "").strip()
    updated_at = _as_float(block.get("updated_at"))
    ttl = _as_float(ttl_hours)
    if not content or ttl <= 0:
        return {"content": content, "updated_at": int(updated_at), "expires_at": 0, "expired": False}
    return {
        "content": content,
        "updated_at": int(updated_at),
        "expires_at": int(updated_at + ttl * 3600) if updated_at > 0 else 0,
        "expired": updated_at <= 0 or now_ts - updated_at > ttl * 3600,
    }


def list_contextual_details(portrait_block: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stored contextual details that have a summary, in stored order (for display)."""
    out: List[Dict[str, Any]] = []
    raw_details = (portrait_block or {}).get("contextual_details") or []
    for item in raw_details if isinstance(raw_details, list) else []:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        raw_keywords = item.get("keywords")
        keywords = [
            str(k).strip() for k in raw_keywords if str(k or "").strip()
        ] if isinstance(raw_keywords, list) else []
        out.append({"summary": summary, "keywords": keywords})
    return out


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


def _human_label(app) -> str:
    """Label for the user's lines in conversation excerpts: config.user_name, else 用户."""
    config = getattr(getattr(app, "state", None), "config", None)
    return (getattr(config, "user_name", "") or "").strip() or "用户"


def _get_recent_turns(storage_root: str, max_turns: int) -> List[Dict[str, Any]]:
    """Return the last max_turns rounds from ConversationStore, each with its timestamp."""
    if max_turns <= 0:
        return []
    try:
        from src.memory.conversation_store import ConversationStore
        store = ConversationStore(storage_root, max_turns=0)
        return store.get_recent_with_ts(max_turns) or []
    except Exception as e:
        logger.warning("[UserPortrait] 读取对话失败: %s", e)
        return []


def _format_conversation(turns: List[Dict[str, Any]], user_label: str) -> str:
    """Format turns as "label: content" lines.

    Every user-role line gets the same current label, including legacy rows without a
    sender and rows saved under an earlier user name, so a prompt that says user lines
    start with that label holds for the whole excerpt.
    """
    label_for_user = (user_label or "").strip() or "用户"
    lines = []
    for turn in turns:
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        if turn.get("role") == "user":
            label = label_for_user
        else:
            label = (turn.get("sender") or "").strip() or "AI"
        lines.append(f"{label}: {content}")
    return "\n".join(lines)


def _get_recent_conversation_text(storage_root: str, max_turns: int, user_label: str = "用户") -> str:
    """Last max_turns rounds from ConversationStore as "label: content" lines."""
    return _format_conversation(_get_recent_turns(storage_root, max_turns), user_label)


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
    current_recent_context: str,
    current_contextual_details: str,
    introduction: str,
    conversation: str,
    day_summaries: str,
    facts: str,
) -> str:
    """对所有输入计算 sha1，用于短路：输入完全没变就跳过 LLM 调用。"""
    h = hashlib.sha1()
    for piece in (
        current_portrait,
        current_recent_context,
        current_contextual_details,
        introduction,
        conversation,
        day_summaries,
        facts,
    ):
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
    # The user's name is only ever the explicit setting; a style-rule form of address is
    # passed separately and phrased as how the character usually addresses them.
    style_address_form = "" if name else extract_style_address_form(card)
    max_conv_turns = int(cfg.get("max_input_conv_turns", 60))
    conversation = _get_recent_conversation_text(storage_root, max_conv_turns, _human_label(app))
    day_summaries = _get_recent_day_summaries_text(storage_root, 7) if cfg.get("use_day_summary_as_input", True) else ""
    facts = _get_facts_text(storage_root, 30) if cfg.get("use_facts_as_input", True) else ""
    current_portrait = (portrait.get("content") or "").strip()
    recent_block = portrait.get("recent_context") or {}
    if isinstance(recent_block, dict):
        current_recent_context = (recent_block.get("content") or "").strip()
    else:
        current_recent_context = str(recent_block).strip()
    current_contextual_details = json.dumps(
        portrait.get("contextual_details") or [],
        ensure_ascii=False,
    )

    if not conversation and not facts and not day_summaries and not current_portrait:
        logger.info("[UserPortrait] 无任何输入，跳过 profile=%s", profile_id)
        return None

    # signature 短路
    sig = _compute_input_signature(
        current_portrait,
        current_recent_context,
        current_contextual_details,
        introduction,
        conversation,
        day_summaries,
        facts,
    )
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

    known_forms = list_address_form_names(portrait)
    if locale == "zh":
        known_address_forms = "、".join(f"「{n}」" for n in known_forms)
    else:
        known_address_forms = ", ".join(f'"{n}"' for n in known_forms)

    def _placeholder(s: str, zh: str, en: str) -> str:
        return s.strip() if s else (zh if locale == "zh" else en)

    system_text = Template(system_prompt).safe_substitute(
        persona_name=persona_name,
        base_prompt=_placeholder(base_prompt_text, "（暂无角色设定可参考）", "(no persona setting available)"),
        user_name=_placeholder(user_name, "（用户尚未设置称呼）", "(no preferred name set)"),
        known_address_forms=_placeholder(
            known_address_forms,
            "（尚无已确认的对方称呼）",
            "(no confirmed forms of address from them yet)",
        ),
        mutual_address=_placeholder(
            format_mutual_address_facts(
                user_name,
                portrait,
                locale=locale,
                style_address_form=style_address_form,
            ),
            "（双方称呼尚未确认）",
            "(mutual address forms not yet confirmed)",
        ),
        introduction=_placeholder(introduction, "（用户尚未填写自我介绍）", "(no self-introduction provided)"),
        current_portrait=_placeholder(current_portrait, "（这是首次生成画像，旧画像为空）", "(no previous portrait — this is the first run)"),
        current_recent_context=_placeholder(
            current_recent_context,
            "（暂无近期状态）",
            "(no recent context)",
        ),
        current_contextual_details=_placeholder(
            current_contextual_details if current_contextual_details != "[]" else "",
            "（暂无按需细节）",
            "(no contextual details)",
        ),
        recent_conversation=_placeholder(conversation, "（无近期对话）", "(no recent conversation)"),
        day_summaries=_placeholder(day_summaries, "（无可用 day_summary）", "(no day_summary available)"),
        memory_facts=_placeholder(facts, "（无可用记忆事实）", "(no memory facts available)"),
        refresh_count=str(int(portrait.get("refresh_count") or 0)),
    )

    user_text = get_prompt("user_portrait.refresh_user", locale=locale) or (
        "请根据以上信息更新三层画像。只输出 JSON 对象，不要任何前缀或解释。"
        if locale == "zh" else
        "Update the three-layer portrait. Output only the JSON object, with no prefix or explanation."
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

    parsed = _parse_portrait_payload(raw)
    if not parsed:
        logger.warning("[UserPortrait] LLM 返回的三层画像无法解析，保留旧画像 profile=%s", profile_id)
        return None

    # ── 写入 ────────────────────────────────────────────────────────────────
    # Reload the card right before writing: a settings save, persona evolution, or an
    # address-form deletion may have been saved while the LLM call was running. Only the
    # portrait fields this refresh owns are replaced; everything else keeps its latest value.
    latest = _load_profile_card(profile_id)
    if not latest:
        logger.warning("[UserPortrait] Profile disappeared during refresh, not saving %s", profile_id)
        return None
    if not (latest.get("user_portrait_config") or {}).get("enabled", True):
        logger.info("[UserPortrait] Portrait was disabled during refresh, not saving profile=%s", profile_id)
        return None
    latest_portrait = latest.get("user_portrait") or {}

    now_ts = int(time.time())
    new_refresh_count = int(latest_portrait.get("refresh_count") or 0) + 1
    new_content = parsed["content"]
    new_recent_context = parsed["recent_context"]
    new_contextual_details = parsed["contextual_details"]
    stored_sig = _compute_input_signature(
        new_content,
        new_recent_context,
        json.dumps(new_contextual_details, ensure_ascii=False),
        introduction,
        conversation,
        day_summaries,
        facts,
    )

    portrait_before = {
        "content": str(latest_portrait.get("content") or "").strip(),
        "recent_context": latest_portrait.get("recent_context") or {},
        "contextual_details": latest_portrait.get("contextual_details") or [],
    }
    new_recent_block = {
        "content": new_recent_context,
        "updated_at": now_ts,
    } if new_recent_context else {}

    latest_portrait.update({
        "content": new_content,
        "recent_context": new_recent_block,
        "contextual_details": new_contextual_details,
        "updated_at": now_ts,
        "refresh_count": new_refresh_count,
        "last_input_signature": stored_sig,
    })
    latest["user_portrait"] = latest_portrait
    _save_profile_card(profile_id, latest)

    entry = {
        "version": new_refresh_count,
        "updated_at": now_ts,
        "triggered_by": triggered_by,
        "content_before": portrait_before["content"],
        "content_after": new_content,
        "portrait_before": portrait_before,
        "portrait_after": {
            "content": new_content,
            "recent_context": new_recent_block,
            "contextual_details": new_contextual_details,
        },
    }
    _append_changelog(storage_root, entry)

    logger.info("[UserPortrait] 画像刷新完成 profile=%s version=%d trigger=%s",
                profile_id, new_refresh_count, triggered_by)

    # Address-form detection shares the portrait's trigger but runs as its own short
    # call; a failure there never affects the portrait result.
    try:
        await refresh_address_forms(profile_id, storage_root, app)
    except Exception as e:
        logger.warning("[UserPortrait] 称呼识别失败（不影响画像）profile=%s: %s", profile_id, e)

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
    before = entry.get("portrait_before")
    if isinstance(before, dict):
        portrait["content"] = str(before.get("content") or "")
        portrait["recent_context"] = before.get("recent_context") or {}
        portrait["contextual_details"] = before.get("contextual_details") or []
    else:
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


def clear_recent_context(profile_id: str) -> bool:
    """Clear the recent-context layer so it is no longer sent to the model.

    Like manual_edit this resets the input signature so the next refresh really runs,
    and like manual_edit it adds no changelog entry.
    """
    card = _load_profile_card(profile_id)
    if not card:
        return False
    portrait = card.get("user_portrait") or {}
    portrait["recent_context"] = {}
    portrait["last_input_signature"] = ""
    card["user_portrait"] = portrait
    _save_profile_card(profile_id, card)
    logger.info("[UserPortrait] Cleared recent context profile=%s", profile_id)
    return True


def delete_contextual_detail(profile_id: str, summary: str) -> bool:
    """Delete one contextual detail, matched by summary (case-insensitive).

    Returns False when the profile or the detail does not exist. Resets the input
    signature like manual_edit; adds no changelog entry.
    """
    target = (summary or "").strip().casefold()
    if not target:
        return False
    card = _load_profile_card(profile_id)
    if not card:
        return False
    portrait = card.get("user_portrait") or {}
    details = portrait.get("contextual_details") or []
    if not isinstance(details, list):
        return False
    kept = [
        d for d in details
        if not (isinstance(d, dict) and str(d.get("summary") or "").strip().casefold() == target)
    ]
    if len(kept) == len(details):
        return False
    portrait["contextual_details"] = kept
    portrait["last_input_signature"] = ""
    card["user_portrait"] = portrait
    _save_profile_card(profile_id, card)
    logger.info("[UserPortrait] Deleted contextual detail profile=%s summary=%s", profile_id, summary)
    return True


# ── Address forms (user_portrait.address_forms) ─────────────────────────────
#
# Why this is kept apart from the portrait text:
#   The portrait is 600-3000 characters of prose that an LLM rewrites on every refresh,
#   so details can get lost, while how the user addresses the character has to stay
#   exact. The portrait prompt's anti-contamination rule (rule 5) also tends to drop
#   unusual forms of address, so a one-letter nickname such as "a" would almost always
#   be lost. A separate short task keeps the prompt focused, its short JSON reply
#   cannot be truncated, and a failure never affects the portrait itself.
#
# Accumulation (the key property of this feature):
#   Forms only accumulate. They do not decay and are not removed for going unused, so
#   a form the user has not used for months is still recognized when it comes back,
#   which recency-weighted facts cannot guarantee. The only way to remove one is a
#   manual delete_address_form(). Each refresh counts only messages newer than the
#   scan watermark (address_forms.scanned_until), so no message is counted twice.

MAX_ADDRESS_FORMS = 8          # max forms kept; beyond this the lowest counts are dropped
MAX_ADDRESS_FORM_LEN = 20      # longer values are treated as malformed model output


def _parse_address_forms(raw: str) -> List[Dict[str, Any]]:
    """Parse the address-form reply. Anything unusable yields [] (prefer missing over wrong)."""
    return _parse_address_forms_or_none(raw) or []


def _parse_address_forms_or_none(raw: str) -> Optional[List[Dict[str, Any]]]:
    """Parse the address-form reply; None when no JSON array can be read at all.

    The array may be followed or preceded by prose. The first array with valid entries
    wins; when arrays decode but none has a valid entry, the result is [].
    """
    decoded_any = False
    for data in _iter_json_values(raw, list):
        decoded_any = True
        forms = _normalize_address_forms(data)
        if forms:
            return forms
    return [] if decoded_any else None


def _normalize_address_forms(data: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        form = str(item.get("form") or "").strip()
        if not form or len(form) > MAX_ADDRESS_FORM_LEN:
            continue
        key = form.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            count = max(1, int(item.get("count") or 1))
        except (TypeError, ValueError):
            count = 1
        out.append({
            "form": form,
            "count": count,
            "evidence": str(item.get("evidence") or "").strip()[:40],
        })
    return out


def _merge_address_forms(existing: List[Dict[str, Any]],
                         found: List[Dict[str, Any]],
                         now_ts: int) -> List[Dict[str, Any]]:
    """Merge this round's results into the table: add to existing counts, append new forms, never drop old ones."""
    by_key = {}
    merged: List[Dict[str, Any]] = []
    for e in existing:
        if not isinstance(e, dict):
            continue
        form = str(e.get("form") or "").strip()
        if not form:
            continue
        rec = {
            "form": form,
            "count": int(e.get("count") or 1),
            "evidence": str(e.get("evidence") or ""),
            "first_seen": int(e.get("first_seen") or now_ts),
            "last_seen": int(e.get("last_seen") or now_ts),
        }
        by_key[form.lower()] = rec
        merged.append(rec)

    for f in found:
        key = f["form"].lower()
        if key in by_key:
            rec = by_key[key]
            rec["count"] += f["count"]
            rec["last_seen"] = now_ts
            if f.get("evidence"):
                rec["evidence"] = f["evidence"]
        else:
            rec = {
                "form": f["form"],
                "count": f["count"],
                "evidence": f.get("evidence", ""),
                "first_seen": now_ts,
                "last_seen": now_ts,
            }
            by_key[key] = rec
            merged.append(rec)

    merged.sort(key=lambda r: (-r["count"], -r["last_seen"]))
    return merged[:MAX_ADDRESS_FORMS]


def _address_forms_enabled(card: Dict[str, Any]) -> bool:
    cfg = card.get("user_portrait_config") or {}
    return bool(cfg.get("enabled", True) and cfg.get("address_forms_enabled", True))


def _address_scan_watermark(block: Dict[str, Any]) -> float:
    """Timestamp of the newest message already counted into the address-form table.

    Tables written before the watermark existed fall back to their updated_at: every
    earlier refresh re-read the recent window, so messages older than the last update
    are already counted and counting them again would inflate the table. A table that
    was never written has no updated_at and starts from 0, scanning the recent window.
    """
    if "scanned_until" in block:
        return _as_float(block.get("scanned_until"))
    return _as_float(block.get("updated_at"))


async def refresh_address_forms(profile_id: str, storage_root: str, app) -> Optional[List[Dict[str, Any]]]:
    """Detect and accumulate how the user addresses this character.

    Only messages newer than the scan watermark are sent, so messages counted by an
    earlier refresh are not counted again. Returns the updated table, or None when
    skipped, when nothing new was found, or on failure.
    """
    card = _load_profile_card(profile_id)
    if not card or not _address_forms_enabled(card):
        return None

    cfg = card.get("user_portrait_config") or {}
    block = (card.get("user_portrait") or {}).get("address_forms") or {}
    watermark = _address_scan_watermark(block)
    max_turns = int(cfg.get("max_input_conv_turns", 60))
    turns = [
        turn for turn in _get_recent_turns(storage_root, max_turns)
        if _as_float(turn.get("timestamp")) > watermark
    ]
    if not any(turn.get("role") == "user" and (turn.get("content") or "").strip() for turn in turns):
        return None
    newest_ts = max(_as_float(turn.get("timestamp")) for turn in turns)
    user_label = _human_label(app)
    conversation = _format_conversation(turns, user_label)

    locale = get_locale()
    tmpl = get_prompt("user_portrait.address_forms", locale=locale)
    if not tmpl:
        logger.warning("[UserPortrait] address_forms prompt 未找到，跳过")
        return None

    persona_name = card.get("display_name") or card.get("name") or profile_id

    existing_names = list_address_form_names(card.get("user_portrait") or {})
    if locale == "zh":
        known_forms_text = "、".join(f"「{n}」" for n in existing_names) or "（尚无）"
    else:
        known_forms_text = ", ".join(f'"{n}"' for n in existing_names) or "(none yet)"

    system_text = Template(tmpl).safe_substitute(
        persona_name=persona_name,
        user_label=user_label,
        conversation=conversation,
        known_address_forms=known_forms_text,
    )
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": "请输出 JSON 数组。" if locale == "zh" else "Output the JSON array."},
    ]

    try:
        raw = await _llm_call(messages, app, role="user_portrait_address_forms", profile_id=profile_id)
    except Exception as e:
        logger.warning("[UserPortrait] 称呼识别 LLM 调用失败 profile=%s: %s", profile_id, e)
        return None

    found = _parse_address_forms_or_none(raw)
    if found is None:
        # Keep the watermark so these messages are scanned again next time.
        logger.warning("[UserPortrait] Address-form reply has no JSON array, will rescan profile=%s", profile_id)
        return None

    # Reload the card right before writing (see trigger_portrait_refresh); only
    # user_portrait.address_forms is replaced.
    latest = _load_profile_card(profile_id)
    if not latest or not _address_forms_enabled(latest):
        return None
    latest_portrait = latest.get("user_portrait") or {}
    latest_block = dict(latest_portrait.get("address_forms") or {})
    if _address_scan_watermark(latest_block) > watermark:
        # Another refresh counted these messages while this call was running.
        logger.debug("[UserPortrait] Another refresh already counted these messages profile=%s", profile_id)
        return None

    now_ts = int(time.time())
    if found:
        latest_block["forms"] = _merge_address_forms(latest_block.get("forms") or [], found, now_ts)
        latest_block["updated_at"] = now_ts
    latest_block["scanned_until"] = newest_ts
    latest_portrait["address_forms"] = latest_block
    latest["user_portrait"] = latest_portrait
    _save_profile_card(profile_id, latest)

    if not found:
        logger.debug("[UserPortrait] 本轮未识别到称呼 profile=%s", profile_id)
        return None
    merged = latest_block["forms"]
    logger.info("[UserPortrait] 称呼表更新 profile=%s: %s",
                profile_id, ", ".join(f'{r["form"]}×{r["count"]}' for r in merged))
    return merged


def get_address_forms(profile_id: str) -> List[Dict[str, Any]]:
    """Read the current address-form table (highest count first)."""
    card = _load_profile_card(profile_id)
    block = ((card.get("user_portrait") or {}).get("address_forms") or {})
    forms = block.get("forms") or []
    return [f for f in forms if isinstance(f, dict) and f.get("form")]


def delete_address_form(profile_id: str, form: str) -> bool:
    """Delete one form of address (the only way to remove one, for correcting misdetections)."""
    target = (form or "").strip().lower()
    if not target:
        return False
    card = _load_profile_card(profile_id)
    if not card:
        return False
    portrait = card.get("user_portrait") or {}
    block = portrait.get("address_forms") or {}
    forms = block.get("forms") or []
    kept = [f for f in forms if str(f.get("form", "")).strip().lower() != target]
    if len(kept) == len(forms):
        return False
    if "scanned_until" not in block and block.get("updated_at"):
        # Freeze the pre-watermark fallback (see _address_scan_watermark) before
        # updated_at changes, so unscanned messages are not skipped later.
        block["scanned_until"] = _as_float(block.get("updated_at"))
    block["forms"] = kept
    block["updated_at"] = int(time.time())
    card.setdefault("user_portrait", {})["address_forms"] = block
    _save_profile_card(profile_id, card)
    logger.info("[UserPortrait] 已删除称呼 profile=%s form=%s", profile_id, form)
    return True
