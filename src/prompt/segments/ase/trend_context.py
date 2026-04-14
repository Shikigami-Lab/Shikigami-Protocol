"""ase_trend_context — 趋势热点注入 ASE prompt。

inject_into="ase"，cooldown 触发（默认 120 分钟），
从 TrendStore.get_recent_raw() 取最新条目，不标记 used，
供 ASE LLM 自主决定是否在主动发言中引用。
"""
import logging
import os

from src.prompt.base import PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)


@register
class AseTrendContextSegment(PromptSegment):
    segment_id = "ase_trend_context"
    inject_into = "ase"
    is_core = False
    is_readonly = True
    label = "趋势热点感知"
    label_en = "Trend Awareness"
    description = "将近期网络热点注入 ASE prompt，供 AI 在主动发言时自然引用"
    description_en = "Injects unused trend items into ASE for topic-driven proactive speech"
    default_trigger_mode = "cooldown"
    default_trigger_param = 120.0   # 分钟，默认 2h

    def build(self, ctx) -> SegmentResult:
        profile = getattr(ctx, "profile", {}) or {}
        trend_cfg = profile.get("trend_config") or {}
        if not trend_cfg.get("enabled", False):
            return SegmentResult(fired=False)

        storage_root = getattr(getattr(ctx, "session", None), "storage_root", "") or ""
        if not storage_root:
            return SegmentResult(fired=False)

        try:
            from src.tools.trends.store import TrendStore
            store = TrendStore(storage_root)
            items = store.get_recent_raw(n=3)
        except Exception as e:
            logger.debug("[AseTrendContext] store error: %s", e)
            return SegmentResult(fired=False)

        if not items:
            return SegmentResult(fired=False)

        from src.config.prompt_loader import get_locale
        locale = get_locale()
        if locale == "en":
            header = "[Recent Topics] You may naturally weave one of these into your message, or ignore them entirely:"
        else:
            header = "[近期话题] 以下是近期热点，你可以在发言中自然融入其中一条，也可以完全不提："

        lines = [header]
        for item in items:
            label = item.source_label
            title = item.title.strip()
            entry = f"· {title}（来源：{label}）" if label else f"· {title}"
            lines.append(entry)

        text = "\n".join(lines)
        logger.debug("[AseTrendContext] injected %d items", len(items))
        return SegmentResult(messages=[{"role": "system", "content": text}])
