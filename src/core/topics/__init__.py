"""Proactive topic discovery: the TopicSource abstraction.

Five sources (trend / conversation_recall / user_life / ai_self / random_api) register through
the registry. Reflection collects candidates from get_enabled_sources(); ASE resolves material
by ID with get_source().
"""
from src.core.topics.base import (TopicCandidate, TopicMaterial, TopicSource,
                                  TopicSourceContext, topic_id_hash)
from src.core.topics.registry import (all_source_ids, get_enabled_sources,
                                      get_source, register_topic_source)
import src.core.topics.sources  # noqa: F401  触发所有来源注册

# topic_pick source → 派生 speak_reason（沿用 reflection 的 _VALID_SPEAK_REASONS 枚举）
SOURCE_TO_SPEAK_REASON = {
    "trend":               "trend_share",
    "conversation_recall": "memory_recall",
    "ai_self":             "emotional_overflow",
    "user_life":           "silence_concern",
    "random_api":          "none",
}

__all__ = [
    "TopicCandidate", "TopicMaterial", "TopicSource", "TopicSourceContext",
    "topic_id_hash", "register_topic_source", "get_source",
    "get_enabled_sources", "all_source_ids", "SOURCE_TO_SPEAK_REASON",
]
