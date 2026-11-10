"""Segment dependency evaluation — engine / tool / feature switches.

A segment class declares e.g. ``requires = ("engine:emotion",)``.  Both the
prompt pipeline (chat + reflection assembly) and the settings API call
``unavailable_reason()`` so that disabling an engine or tool

1. removes the dependent segment from the prompt, and
2. surfaces a human-readable reason in the UI (checkbox locked).

Keep reason strings short and actionable; they are rendered next to the
segment's checkbox in settings.  All evaluation is read-only and must never
raise — any failure degrades to "available" so a broken subsystem cannot
silently wipe the prompt.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Engines that default ON (disabled only by an explicit `enabled: false`),
# matching the engines' own `cfg.get("enabled", True) is False` convention.
_ENGINES_DEFAULT_ON = frozenset({"emotion", "affinity", "energy"})
# Engines that default OFF (enabled only by an explicit `enabled: true`),
# matching reflection.py / ase.py's `if not cfg.get("enabled")` convention.
_ENGINES_DEFAULT_OFF = frozenset({"reflection", "ase"})

_ENGINE_LABELS = {
    "emotion": "情感引擎",
    "affinity": "好感度引擎",
    "energy": "能量引擎",
    "reflection": "自省引擎",
    "ase": "主动发言引擎",
}


def _engine_enabled(app, profile_id: str, name: str) -> bool:
    from src.config.effective_config import get_effective_engine_config

    cfg = get_effective_engine_config(app, profile_id, name)
    if name in _ENGINES_DEFAULT_ON:
        return cfg.get("enabled", True) is not False
    # default-off engines: truthy enabled means on
    return bool(cfg.get("enabled"))


def _agent_runtime(app):
    return getattr(getattr(app, "state", None), "agent_runtime", None)


def _memory_enabled(app, profile: Dict[str, Any]) -> bool:
    """Mirror MemoryManager's merge: default off → app memory config → profile."""
    config = getattr(getattr(app, "state", None), "config", None)
    if config is None:
        return False
    enabled = (config.get_memory_config() or {}).get("enabled", False)
    prof_cfg = (profile or {}).get("memory_config") or {}
    if prof_cfg.get("enabled") is not None:
        enabled = prof_cfg["enabled"]
    return bool(enabled)


def unavailable_reason(
    requires: Tuple[str, ...],
    app,
    profile_id: str,
    profile: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Return a short Chinese reason if any dependency is unmet, else None.

    ``profile`` is the loaded character card (for tool_configs / memory_config
    overrides); callers without a card may pass None and it is loaded lazily.
    """
    if not requires:
        return None
    if app is None:
        # Without app state we cannot prove availability of anything backed
        # by runtime/config; treat engine deps as available (their own
        # defaults) but gate runtime-backed ones conservatively off?  No —
        # settings pages always have app; pipeline always has app.  A missing
        # app means a synthetic/test build: stay permissive.
        return None
    try:
        for key in requires:
            reason = _check_one(key, app, profile_id, profile)
            if reason:
                return reason
    except Exception as e:  # never let dependency math break prompting
        logger.warning("[dependencies] evaluation failed for %s: %s", requires, e)
        return None
    return None


def _load_profile(profile_id: str) -> Dict[str, Any]:
    from src.config.profile_loader import load_profile_card

    return load_profile_card(profile_id)


def _check_one(key: str, app, profile_id: str, profile: Optional[Dict[str, Any]]) -> Optional[str]:
    kind, _, name = key.partition(":")

    if kind == "engine":
        if not _engine_enabled(app, profile_id, name):
            return f"{_ENGINE_LABELS.get(name, name)}已关闭（行为设置）"
        return None

    if kind == "feature" and name == "memory":
        if profile is None:
            profile = _load_profile(profile_id)
        if not _memory_enabled(app, profile):
            return "记忆系统已关闭（记忆设置）"
        return None

    if kind == "feature" and name == "agent_tools":
        runtime = _agent_runtime(app)
        if runtime is None or not runtime.tools_allowed(profile_id):
            return "AI 工具未启用（工具设置）"
        return None

    if kind == "capability" and name == "workspace":
        runtime = _agent_runtime(app)
        if runtime is None or not runtime.tools_allowed(profile_id):
            return "AI 工具未启用（工具设置）"
        if not runtime.access_for(profile_id).get("workspace_enabled"):
            return "工作区权限未开启（工具设置）"
        return None

    if kind == "tool":
        if profile is None:
            profile = _load_profile(profile_id)
        tool_cfgs = (profile or {}).get("tool_configs") or {}
        if name == "timer":
            runtime = _agent_runtime(app)
            if runtime is None or not runtime.timer_allowed(profile_id):
                return "计时器工具未启用（工具/人格设置）"
            return None
        if name == "todo":
            if ((tool_cfgs.get("todo") or {}).get("enabled", True)) is not True:
                return "待办工具已关闭（人格 → 工具配置）"
            return None
        if name == "weather":
            if ((tool_cfgs.get("weather") or {}).get("enabled", False)) is not True:
                return "天气工具未启用（人格 → 工具配置）"
            return None

    logger.debug("[dependencies] unknown requires key: %s", key)
    return None
