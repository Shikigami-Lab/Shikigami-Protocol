"""主动话题发现 —— TopicSource 抽象层。

5 个来源（trend / conversation_recall / user_life / ai_self / random_api）通过
registry 注册。自省遍历 get_enabled_sources() 收集候选，ASE 用 get_source()
按 ID 解析素材。详见 docs/superpowers/specs/2026-05-18-proactive-topic-discovery-design.md
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
