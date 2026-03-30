"""趋势感知 → Reflection 注入段落。

targets_into="reflection"，从 TrendStore.get_unused() 取未读条目注入 reflection prompt，
让 Reflection LLM 在生成 topic_hint 时能自主引用网络动态。

条目标记为 used 后 3 天清理，防止重复提及。
trend_context=None（没有数据或功能未启用）时行为与现在完全一致。
"""
import logging
import os

from src.prompt.base import ReflectionBuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)


@register
class TrendContextReflectionSegment(PromptSegment):
    segment_id = "reflection_trend_context"
    priority = 55          # 在 reflection 各段落中较靠后，persona 相关段落在前
    label = "趋势感知（自省）"
    description = "将未读趋势条目注入自省 prompt，辅助生成 topic_hint"
    is_core = False
    inject_into = "reflection"
    is_readonly = True
    default_trigger_mode = "always"

    def build(self, ctx: "ReflectionBuildContext") -> SegmentResult:  # type: ignore[override]
        profile_id = getattr(ctx, "profile_id", "")
        profile = getattr(ctx, "profile", {}) or {}

        trend_cfg = profile.get("trend_config") or {}
        if not trend_cfg.get("enabled", False):
            return SegmentResult(fired=False)

        # 通过 profile_id 找 storage_root
        storage_root = os.path.join("profiles", profile_id)
        if not os.path.isdir(storage_root):
            return SegmentResult(fired=False)

        try:
            from src.tools.trends.store import TrendStore
            store = TrendStore(storage_root)
            items = store.get_unused(n=3)
        except Exception as e:
            logger.debug("[TrendContextReflection] store error: %s", e)
            return SegmentResult(fired=False)

        if not items:
            return SegmentResult(fired=False)

        lines = ["[近期网络动态（供自省参考，可自主决定是否引用）]"]
        for item in items:
            label = item.source_label
            title = item.title.strip()
            entry = f"· {title}（来源：{label}）" if label else f"· {title}"
            lines.append(entry)

        text = "\n".join(lines)
        logger.debug("[TrendContextReflection] injected %d items for profile=%s", len(items), profile_id)
        return SegmentResult(
            messages=[{"role": "system", "content": text}],
        )
