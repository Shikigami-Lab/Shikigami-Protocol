"""TopicSource 注册表 —— 仿 src/prompt/registry.py。"""
from __future__ import annotations

import logging
from typing import Optional, Type

from src.core.topics.base import TopicSource

logger = logging.getLogger(__name__)

_registry: dict[str, Type[TopicSource]] = {}


def register_topic_source(cls: Type[TopicSource]) -> Type[TopicSource]:
    """类装饰器 —— 注册一个 TopicSource 子类。"""
    if not cls.source_id:
        raise ValueError(f"TopicSource {cls.__name__} 必须定义 source_id")
    if cls.source_id in _registry:
        logger.warning("[topics] source_id '%s' 已注册，覆盖", cls.source_id)
    _registry[cls.source_id] = cls
    return cls


def get_source(source_id: str) -> Optional[TopicSource]:
    """按 ID 取来源实例；不存在返回 None。"""
    cls = _registry.get(source_id)
    return cls() if cls else None


def get_enabled_sources(td_config: dict) -> list[TopicSource]:
    """返回 topic_discovery 配置中启用的来源实例列表。"""
    src_cfg = (td_config or {}).get("sources") or {}
    out: list[TopicSource] = []
    for source_id, cls in _registry.items():
        entry = src_cfg.get(source_id)
        if entry is None:
            enabled = cls.enabled_by_default
        else:
            enabled = bool(entry.get("enabled", cls.enabled_by_default))
        if enabled:
            out.append(cls())
    return out


def all_source_ids() -> list[str]:
    return list(_registry.keys())
