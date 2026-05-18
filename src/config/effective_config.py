"""Resolve effective config per profile: global + profile overrides."""
import json
import logging
import os
from typing import Any, Dict

from src.utils.paths import get_project_root

logger = logging.getLogger(__name__)

PROFILES_DIR = os.path.join(get_project_root(), "profiles")


def _load_profile_card(profile_id: str) -> Dict[str, Any]:
    path = os.path.join(PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("[effective_config] failed to load profile %s: %s", profile_id, e)
        return {}


def get_effective_max_history_turns(app, profile_id: str) -> int:
    """先读 profile 的 memory_config.max_history_turns，若无则读全局 memory 或顶层，默认 20。"""
    card = _load_profile_card(profile_id)
    mc = card.get("memory_config") or {}
    if mc.get("max_history_turns") is not None:
        v = int(mc["max_history_turns"])
        return max(1, min(200, v))
    config = app.state.config
    mem = config.get_memory_config()
    if mem.get("max_history_turns") is not None:
        v = int(mem["max_history_turns"])
        return max(1, min(200, v))
    return max(1, min(200, getattr(config, "max_history_turns", 20)))


def get_effective_topic_discovery_config(app, profile_id: str) -> Dict[str, Any]:
    """全局 topic_discovery 配置 + 人格级 enabled 三态覆盖。

    人格卡 engine_overrides.topic_discovery.enabled 为 true/false 时覆盖全局，
    缺失或 null 时跟随全局。来源级开关仅全局，不做 per-profile。
    """
    cfg = dict(app.state.config.get_topic_discovery_config())
    card = _load_profile_card(profile_id)
    override = (card.get("engine_overrides") or {}).get("topic_discovery") or {}
    if override.get("enabled") is not None:
        cfg["enabled"] = bool(override["enabled"])
    return cfg


def get_effective_engine_config(app, profile_id: str, engine: str) -> Dict[str, Any]:
    """合并全局 engines[engine] 与 profile 的 engine_overrides[engine]。"""
    config = app.state.config
    global_cfg = config.get_engine_config(engine)
    card = _load_profile_card(profile_id)
    overrides = card.get("engine_overrides") or {}
    per_engine = overrides.get(engine) or {}
    return {**global_cfg, **per_engine}
