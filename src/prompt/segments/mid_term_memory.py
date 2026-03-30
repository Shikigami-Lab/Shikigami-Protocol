"""中期记忆 Segment（按天对话摘要）— priority=88

注入最近 3 天的对话摘要，使用模糊相对日期描述。
day_summary_enabled=False 时静默跳过。
"""
import logging
from datetime import date, datetime

from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)


def _relative_date(date_str: str) -> str:
    """将 YYYY-MM-DD 转为模糊相对日期（昨天、X天前、上周等）。"""
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return date_str
    today = date.today()
    diff = (today - target).days
    if diff == 0:
        return "今天"
    if diff == 1:
        return "昨天"
    if diff < 7:
        return f"{diff}天前"
    if diff < 14:
        return "上周"
    weeks = diff // 7
    if weeks < 5:
        return f"约{weeks}周前"
    months = diff // 30
    return f"约{months}个月前"


@register
class MidTermMemorySegment(PromptSegment):
    segment_id = "mid_term_memory"
    priority = 88
    label = "中期记忆（对话摘要）"
    description = "注入最近几天的对话主题和情绪基调摘要"
    is_core = False
    default_trigger_mode = "always"

    def build(self, ctx: BuildContext) -> SegmentResult:
        app = ctx.extras.get("app")
        if not app:
            return SegmentResult(fired=False)

        mgr = _get_manager(app, ctx)
        if not mgr or not mgr._cfg.get("day_summary_enabled"):
            return SegmentResult(fired=False)

        summaries = mgr.day_store.get_recent(n=3)
        if not summaries:
            return SegmentResult(fired=False)

        lines = []
        for s in summaries:
            label = _relative_date(s.get("date", ""))
            lines.append(f"- {label}：{s.get('summary', '')}")

        content = "【近期对话回顾】\n" + "\n".join(lines)
        return SegmentResult(messages=[{"role": "system", "content": content}])


def _get_manager(app, ctx: BuildContext):
    managers = getattr(app.state, "memory_managers", {})
    return managers.get(ctx.session.profile_id)
