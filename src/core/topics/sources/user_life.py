"""user_life —— 用户生活话题来源。

从长期记忆事实（MemoryManager.get_reflection_context_facts）取候选。
事实无天然稳定 ID，用 content 的短哈希作 item_id；resolve 重取事实并按哈希匹配。
素材静态。去重靠 proactive_log。
"""
import logging
from typing import Optional

from src.core.topics.base import (TopicCandidate, TopicMaterial, TopicSource,
                                  TopicSourceContext, topic_id_hash)
from src.core.topics.registry import register_topic_source

logger = logging.getLogger(__name__)


@register_topic_source
class UserLifeTopicSource(TopicSource):
    source_id = "user_life"
    label = "用户生活"
    label_en = "User's Life"
    enabled_by_default = True
    framing_hint = ("用对对方近况的关心和好奇切入，像惦记着朋友生活的人，"
                    "自然问起、不要像在背诵档案。")
    framing_hint_en = ("Lead with genuine care and curiosity about how they're doing — "
                       "ask naturally, never recite it like a file.")

    def _facts(self, ctx: TopicSourceContext) -> list[dict]:
        mm = ctx.memory_manager
        if mm is None:
            return []
        try:
            return mm.get_reflection_context_facts(max_count=8, min_weight=0.5) or []
        except Exception as e:
            logger.debug("[topics.user_life] 读取事实失败: %s", e)
            return []

    def get_candidates(self, ctx: TopicSourceContext) -> list[TopicCandidate]:
        out: list[TopicCandidate] = []
        for f in self._facts(ctx):
            content = (f.get("content") or "").strip()
            if not content:
                continue
            fid = topic_id_hash(content)
            if f"user_life:{fid}" in ctx.recently_used:
                continue
            summary = content[:60] + ("…" if len(content) > 60 else "")
            out.append(TopicCandidate("user_life", fid, summary))
        return out

    def resolve(self, item_id: str, ctx: TopicSourceContext) -> Optional[TopicMaterial]:
        for f in self._facts(ctx):
            content = (f.get("content") or "").strip()
            if content and topic_id_hash(content) == item_id:
                return TopicMaterial("user_life", item_id, content,
                                     meta={"date": f.get("date", "")})
        return None
