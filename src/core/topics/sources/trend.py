"""trend —— 网络趋势话题来源。包现有 TrendStore。

候选用 get_unused_raw()（只读，不消耗）；resolve 用 get_by_id()；
被 ASE 采用后才用 mark_used() 真正标记。条目被新趋势顶掉 / 已用 → resolve 返回 None。
"""
import logging
from typing import Optional

from src.core.topics.base import (TopicCandidate, TopicMaterial, TopicSource,
                                  TopicSourceContext)
from src.core.topics.registry import register_topic_source

logger = logging.getLogger(__name__)


@register_topic_source
class TrendTopicSource(TopicSource):
    source_id = "trend"
    label = "网络趋势"
    label_en = "Web Trends"
    enabled_by_default = True
    framing_hint = ("聊这条动态时要有你自己的反应和看法，像朋友间随口分享见闻，"
                    "不要像念新闻稿（“我看到一条新闻说……”是错误示范）。")
    framing_hint_en = ("React to this with your own opinion, like a friend casually "
                       "sharing something — never read it out like a news anchor.")

    def _store(self, ctx: TopicSourceContext):
        from src.tools.trends.store import TrendStore
        return TrendStore(ctx.storage_root)

    def _trend_enabled(self, ctx: TopicSourceContext) -> bool:
        return bool((ctx.profile.get("trend_config") or {}).get("enabled", False))

    def get_candidates(self, ctx: TopicSourceContext) -> list[TopicCandidate]:
        if not self._trend_enabled(ctx):
            return []
        items = self._store(ctx).get_unused_raw(n=3)
        out: list[TopicCandidate] = []
        for it in items:
            if f"trend:{it.id}" in ctx.recently_used:
                continue
            summary = it.title.strip()
            if it.source_label:
                summary += f"（来源：{it.source_label}）"
            out.append(TopicCandidate("trend", it.id, summary))
        return out

    def resolve(self, item_id: str, ctx: TopicSourceContext) -> Optional[TopicMaterial]:
        if not self._trend_enabled(ctx):
            return None
        it = self._store(ctx).get_by_id(item_id)
        if it is None or it.used:
            return None
        material = it.title.strip()
        if (it.snippet or "").strip():
            material += "\n" + it.snippet.strip()
        return TopicMaterial("trend", item_id, material,
                             meta={"source_label": it.source_label})

    def mark_used(self, item_id: str, ctx: TopicSourceContext) -> None:
        try:
            self._store(ctx).mark_used(item_id)
        except Exception as e:
            logger.debug("[topics.trend] mark_used 失败: %s", e)
