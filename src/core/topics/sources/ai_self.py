"""ai_self —— AI 自身状态话题来源。

素材是生成式的：由当前情绪 + 自省最近的 thought 组装，item_id 固定 'current'。
永远新鲜，无需 store。
"""
import logging
from typing import Optional

from src.core.topics.base import (TopicCandidate, TopicMaterial, TopicSource,
                                  TopicSourceContext)
from src.core.topics.registry import register_topic_source

logger = logging.getLogger(__name__)


@register_topic_source
class AiSelfTopicSource(TopicSource):
    source_id = "ai_self"
    label = "AI 自身"
    label_en = "The AI Itself"
    enabled_by_default = True
    framing_hint = ("主动分享你自己此刻的状态、心情或念头，"
                    "像主动跟朋友说说自己最近怎么样。")
    framing_hint_en = ("Proactively share your own current state, mood or thoughts — "
                       "like telling a friend how you've been.")

    def _primary_emotion(self, ctx: TopicSourceContext) -> str:
        emo = ctx.emotion or {}
        return (emo.get("primary_emotion") or "").strip()

    def get_candidates(self, ctx: TopicSourceContext) -> list[TopicCandidate]:
        if "ai_self:current" in ctx.recently_used:
            return []
        thought = (ctx.reflection_thought or "").strip()
        primary = self._primary_emotion(ctx)
        if not thought and not primary:
            return []
        if thought:
            summary = "你自己：" + thought[:50] + ("…" if len(thought) > 50 else "")
        else:
            summary = f"你自己：此刻情绪偏向{primary}"
        return [TopicCandidate("ai_self", "current", summary)]

    def resolve(self, item_id: str, ctx: TopicSourceContext) -> Optional[TopicMaterial]:
        thought = (ctx.reflection_thought or "").strip()
        primary = self._primary_emotion(ctx)
        parts: list[str] = []
        if thought:
            parts.append(f"你最近一直在想：{thought}")
        if primary:
            parts.append(f"你此刻的情绪偏向：{primary}")
        if not parts:
            return None
        return TopicMaterial("ai_self", "current", "\n".join(parts))
