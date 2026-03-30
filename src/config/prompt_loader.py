"""prompt_loader.py — 从 config/prompts/*.yaml 加载 LLM 指令。

用法：
    from src.config.prompt_loader import get_prompt, render, get_raw, get_locale

    # 获取当前 locale（从 config/ui_prefs.json）
    locale = get_locale()          # "zh" | "en"

    # 纯静态 prompt（自动用当前 locale）
    text = get_prompt("vlm.screenshot")

    # 带变量的 prompt
    text = render("memory.day_summary", date_str="2026-03-14", conv_text="...")

    # 显式指定 locale
    text = get_prompt("reflection.user_instruction", locale="en")

    # 获取原始值（不做 locale 解析，供需要完整结构的调用方）
    raw = get_raw("time_context.periods")   # list of dicts

YAML 更新后调用 reload() 可热重载，无需重启服务。
"""
import json
import logging
from pathlib import Path
from string import Template
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path("config/prompts")
_PREFS_FILE  = Path("config/ui_prefs.json")
_cache: dict | None = None


# ── 加载 ──────────────────────────────────────────────────────────────────────

def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    merged: dict = {}
    if not _PROMPTS_DIR.exists():
        logger.warning("[PromptLoader] %s not found — all prompts use fallback defaults", _PROMPTS_DIR)
        _cache = merged
        return merged
    for f in sorted(_PROMPTS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            merged.update(data)
        except Exception as e:
            logger.error("[PromptLoader] Failed to load %s: %s", f.name, e)
    _cache = merged
    return _cache


def reload() -> None:
    """强制从磁盘重新读取所有 yaml（热重载，无需重启）。"""
    global _cache
    _cache = None
    logger.info("[PromptLoader] Reloaded all prompts from %s", _PROMPTS_DIR)


# ── Locale ────────────────────────────────────────────────────────────────────

def get_locale() -> str:
    """从 config/ui_prefs.json 读取当前 locale，默认 'zh'。"""
    try:
        with open(_PREFS_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("locale", "zh") or "zh"
    except Exception:
        return "zh"


# ── 导航 ──────────────────────────────────────────────────────────────────────

def _navigate(key_path: str) -> Any:
    """按点分路径在加载后的 dict 中取值；找不到返回 None。"""
    data = _load()
    val: Any = data
    for k in key_path.split("."):
        if not isinstance(val, dict):
            return None
        val = val.get(k)
        if val is None:
            return None
    return val


def _resolve(val: Any, locale: str) -> Any:
    """若 val 是包含 zh/en 键的 dict，返回对应 locale 值（缺失则 fallback zh）。"""
    if isinstance(val, dict) and ("zh" in val or "en" in val):
        return val.get(locale) or val.get("zh")
    return val


# ── 公开 API ──────────────────────────────────────────────────────────────────

def get_prompt(key_path: str, locale: str = None, default: str = "") -> str:
    """按点分路径获取 prompt 字符串，自动解析 zh/en 双语叶节点。

    若 locale=None，自动从 ui_prefs.json 读取。
    若值为 list，用 '\\n' 拼接后返回字符串。
    若路径不存在，返回 default（空字符串）。
    """
    if locale is None:
        locale = get_locale()
    val = _navigate(key_path)
    if val is None:
        return default
    resolved = _resolve(val, locale)
    if resolved is None:
        return default
    if isinstance(resolved, list):
        return "\n".join(str(x) for x in resolved)
    return str(resolved)


def render(key_path: str, locale: str = None, default: str = "", **kwargs: Any) -> str:
    """获取 prompt 并用 string.Template.safe_substitute 替换变量。

    等价于：Template(get_prompt(key_path, locale, default)).safe_substitute(**kwargs)
    缺失变量保留原样（safe_substitute 不抛出 KeyError）。
    """
    if locale is None:
        locale = get_locale()
    template_str = get_prompt(key_path, locale=locale, default=default)
    if not template_str:
        return default
    return Template(template_str).safe_substitute(**kwargs)


def get_raw(key_path: str) -> Any:
    """返回 YAML 原始值（不做 locale 解析）。供需要完整结构的调用方使用。
    例如 get_raw("time_context.periods") 返回 list of dicts。
    """
    return _navigate(key_path)


def get_dict(key_path: str, locale: str = None) -> dict:
    """获取一个 dict 值，并对每个子 value 做 locale 解析。

    用于 emotion_prompts、affinity_prompts 等 {key: {zh:…, en:…}} 结构。
    """
    if locale is None:
        locale = get_locale()
    val = _navigate(key_path)
    if not isinstance(val, dict):
        return {}
    result = {}
    for k, v in val.items():
        resolved = _resolve(v, locale)
        if resolved is None:
            result[k] = v
        elif isinstance(resolved, list):
            result[k] = "\n".join(str(x) for x in resolved)
        else:
            result[k] = resolved
    return result
