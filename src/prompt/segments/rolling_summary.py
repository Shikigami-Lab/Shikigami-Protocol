"""会话内滚动摘要 Segment — priority=87

注入"今天早些时候"的滚动压缩摘要（最近一次 60 轮原文之前的部分）。
rolling_summary_enabled=False、无摘要或摘要属于昨天（尚未交接）时静默跳过。
"""
import logging
from datetime import date, datetime

from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)


@register
class RollingSummarySegment(PromptSegment):
    segment_id = "rolling_summary"
    priority = 87
    label = "会话内滚动摘要"
    description = "注入当天早些时候的对话压缩摘要（窗口外旧对话）"
    is_core = False
    is_readonly = True
    requires = ("feature:memory",)
    default_trigger_mode = "always"
    inject_into = "chat"

    def build(self, ctx: BuildContext) -> SegmentResult:
        app = ctx.extras.get("app")
        if not app:
            return SegmentResult(fired=False)

        mgr = _get_manager(app, ctx)
        if not mgr or not mgr._cfg.get("rolling_summary_enabled"):
            return SegmentResult(fired=False)

        data = mgr.rolling.get()
        if not data or not data.get("summary"):
            return SegmentResult(fired=False)
        today = date.today().strftime("%Y-%m-%d")
        if data.get("date") != today:
            # 属于昨天的摘要：等待日摘要交接或重置，不在今天注入
            return SegmentResult(fired=False)

        updated_at = data.get("updated_at")
        try:
            hhmm = datetime.fromtimestamp(float(updated_at)).strftime("%H:%M")
        except Exception:
            hhmm = ""

        content = (
            f"【今天早些时候】（截至 {hhmm} 的滚动摘要，原话可用历史检索回查）\n"
            f"{data['summary']}"
        )
        return SegmentResult(messages=[{"role": "system", "content": content}])


def _get_manager(app, ctx: BuildContext):
    managers = getattr(app.state, "memory_managers", {})
    return managers.get(ctx.session.profile_id)
