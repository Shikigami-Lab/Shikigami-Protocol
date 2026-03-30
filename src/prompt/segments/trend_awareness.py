"""TrendAwarenessSegment — priority=78，注入近期网络动态供 AI 吸收。

cooldown 模式（默认 4h），避免每条消息都注入。
Segment 在 pipeline.py 中 import 触发 @register，出现在「段落」设置列表。
"""
import logging
import os

from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)


@register
class TrendAwarenessSegment(PromptSegment):
    segment_id = "trend_awareness"
    priority = 78
    label = "近期话题感知"
    description = "将近期网络动态注入 prompt，供 AI 结合自身性格自然引用"
    is_core = False
    inject_into = "chat"
    is_readonly = True
    default_trigger_mode = "cooldown"
    default_trigger_param = 240.0   # 分钟，默认 4h

    def build(self, ctx: BuildContext) -> SegmentResult:
        # 读取 tool_configs 判断是否启用
        tool_cfg = (ctx.profile or {}).get("trend_config") or {}
        if not tool_cfg.get("enabled", False):
            return SegmentResult(fired=False)

        storage_root = getattr(ctx.session, "storage_root", "")
        if not storage_root:
            return SegmentResult(fired=False)

        try:
            from src.tools.trends.store import TrendStore
            store = TrendStore(storage_root)
            items = store.get_recent_raw(n=5)
        except Exception as e:
            logger.debug("[TrendAwareness] store read error: %s", e)
            return SegmentResult(fired=False)

        if not items:
            return SegmentResult(fired=False)

        lines = ["[近期网络动态]"]
        for item in items:
            label = item.source_label
            title = item.title.strip()
            entry = f"· {title}（来源：{label}）" if label else f"· {title}"
            lines.append(entry)

        text = "\n".join(lines)
        logger.debug("[TrendAwareness] injected %d items", len(items))
        return SegmentResult(
            messages=[{"role": "system", "content": text}],
        )
