"""conversation_recall —— 对话回忆话题来源。

从 DaySummaryStore 的历史每日摘要里取候选；item_id 为日期字符串。
素材静态（旧事不会变陈），resolve 即按日期重读摘要。
依赖「每日摘要」功能开启；未开启则候选为空。
去重靠 proactive_log（mark_used 留空）。
"""
import logging
from typing import Optional

from src.core.topics.base import (TopicCandidate, TopicMaterial, TopicSource,
                                  TopicSourceContext)
from src.core.topics.registry import register_topic_source

logger = logging.getLogger(__name__)


@register_topic_source
class ConversationRecallTopicSource(TopicSource):
    source_id = "conversation_recall"
    label = "对话回忆"
    label_en = "Conversation Recall"
    enabled_by_default = True
    framing_hint = ("这是一次旧话题回访，用“上次你提到……后来怎么样了”这样的口吻"
                    "自然接续，体现你记得对方说过的事。")
    framing_hint_en = ("This is revisiting an earlier topic — pick it up naturally "
                       "like 'you mentioned X before, how did that go?'")

    def _store(self, ctx: TopicSourceContext):
        from src.memory.day_store import DaySummaryStore
        return DaySummaryStore(ctx.storage_root)

    def get_candidates(self, ctx: TopicSourceContext) -> list[TopicCandidate]:
        try:
            recent = self._store(ctx).get_recent(n=5)
        except Exception as e:
            logger.debug("[topics.conversation_recall] 读取摘要失败: %s", e)
            return []
        out: list[TopicCandidate] = []
        for s in recent:
            date = (s.get("date") or "").strip()
            summ = (s.get("summary") or "").strip()
            if not date or not summ:
                continue
            if f"conversation_recall:{date}" in ctx.recently_used:
                continue
            short = summ[:60] + ("…" if len(summ) > 60 else "")
            out.append(TopicCandidate("conversation_recall", date,
                                      f"{date} 你和对方聊过：{short}"))
        return out

    def resolve(self, item_id: str, ctx: TopicSourceContext) -> Optional[TopicMaterial]:
        try:
            for s in self._store(ctx).get_all():
                if (s.get("date") or "") == item_id:
                    summ = (s.get("summary") or "").strip()
                    if summ:
                        return TopicMaterial("conversation_recall", item_id, summ,
                                             meta={"date": item_id})
        except Exception as e:
            logger.debug("[topics.conversation_recall] resolve 失败: %s", e)
        return None
