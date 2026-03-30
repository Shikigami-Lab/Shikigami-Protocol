"""世界书条目：默认触发方式（书本级）+ 每条可继承或覆盖。

存储：
  lorebook_settings.default_entry_mode: \"keyword\" | \"constant\"（默认 keyword）
  每条 entry.mode: \"inherit\" | \"keyword\" | \"constant\"
  旧数据仅有 entry.constant 布尔、无 mode 时，视为显式 keyword/constant（非继承）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

_DEFAULT_DM = "keyword"


def effective_entry_mode(entry: dict, lb_settings: dict) -> str:
    """返回 \"keyword\" 或 \"constant\"，用于关键词扫描与常驻判定。"""
    dm = (lb_settings or {}).get("default_entry_mode", _DEFAULT_DM)
    if dm not in ("keyword", "constant"):
        dm = _DEFAULT_DM
    m = entry.get("mode")
    if m == "inherit":
        return "constant" if dm == "constant" else "keyword"
    if m == "constant":
        return "constant"
    if m == "keyword":
        return "keyword"
    # 旧数据：无 mode，用 constant 布尔
    if bool(entry.get("constant")):
        return "constant"
    return "keyword"


def entry_is_constant(entry: dict, lb_settings: dict) -> bool:
    return effective_entry_mode(entry, lb_settings) == "constant"


def normalize_lorebook_entry_for_storage(e: Any, lb_defaults: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """校验并写入 lorebooks/*.json / 内嵌 lorebook 的单条结构。"""
    if not isinstance(e, dict):
        return None
    content = (e.get("content") or "").strip()
    if not content:
        return None

    keys_raw = e.get("keys")
    if isinstance(keys_raw, str):
        keys = [x.strip() for x in re.split(r"[\n,，、]+", keys_raw) if x.strip()]
    elif isinstance(keys_raw, list):
        keys = [str(x).strip() for x in keys_raw if str(x).strip()]
    else:
        keys = []

    lb_defaults = lb_defaults or {}
    dm = lb_defaults.get("default_entry_mode", _DEFAULT_DM)
    if dm not in ("keyword", "constant"):
        dm = _DEFAULT_DM

    raw_mode = e.get("mode")
    if raw_mode not in ("inherit", "keyword", "constant"):
        raw_mode = None
    if raw_mode is None:
        # 旧数据：无 mode → 由 constant 显式决定
        raw_mode = "constant" if bool(e.get("constant")) else "keyword"

    eff = effective_entry_mode({"mode": raw_mode, "constant": e.get("constant")}, {"default_entry_mode": dm})
    is_constant = eff == "constant"
    if not is_constant and not keys:
        return None

    out: Dict[str, Any] = {
        "keys": keys,
        "content": content,
        "enabled": bool(e.get("enabled", True)),
        "constant": is_constant,
        "mode": raw_mode,
    }
    label = (e.get("label") or "").strip()
    if label:
        out["label"] = label
    return out
