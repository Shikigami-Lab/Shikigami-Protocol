"""会话内滚动摘要 Segment — priority=87

注入滚动压缩摘要（最近 keep_turns 轮原文之前、已被压缩的部分）。
摘要与日期无关：只展示生成时间；rolling_summary_enabled=False、无摘要、
或段在 prompt 设置里被禁用时静默跳过。
"""
import logging
from datetime import datetime

from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)


@register
class RollingSummarySegment(PromptSegment):
    segment_id = "rolling_summary"
    priority = 87
    label = "会话内滚动摘要"
    description = "注入对话较早部分的压缩摘要（窗口外旧对话）"
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

        store = getattr(ctx.session, "conversation_store", None)
        data = mgr.rolling_data_for(store) if store is not None else mgr.rolling.get()
        if not data or not data.get("summary"):
            return SegmentResult(fired=False)

        try:
            stamp = datetime.fromtimestamp(float(data.get("updated_at"))).strftime("%m-%d %H:%M")
        except Exception:
            stamp = ""

        content = (
            f"【早些时候的对话摘要】（压缩于 {stamp}，原话可用历史检索回查）\n"
            f"{data['summary']}"
        )
        return SegmentResult(messages=[{"role": "system", "content": content}])


def _get_manager(app, ctx: BuildContext):
    managers = getattr(app.state, "memory_managers", {})
    return managers.get(ctx.session.profile_id)
