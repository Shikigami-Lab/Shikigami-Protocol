"""ActiveTimersSegment — injects running timers into LLM context."""
import logging
import time

from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)


@register
class ActiveTimersSegment(PromptSegment):
    segment_id = "active_timers"
    label = "进行中的计时器"
    description = "当有活跃计时器时注入倒计时信息（always触发，无计时器时自动为空）"
    priority = 48
    default_trigger_mode = "always"

    def build(self, ctx: BuildContext) -> SegmentResult:
        try:
            from src.tools.timer.manager import list_timers
            timers = list_timers()
        except Exception as e:
            logger.warning("[ActiveTimersSegment] list_timers failed: %s", e)
            return SegmentResult(messages=[])

        if not timers:
            return SegmentResult(messages=[])

        lines = []
        for t in timers:
            remaining = max(0, int(t["end_timestamp"] - time.time()))
            mins = remaining // 60
            secs = remaining % 60
            time_str = f"{mins}分{secs}秒" if mins else f"{secs}秒"
            lines.append(f"- 「{t['label']}」还剩 {time_str}")

        text = "【进行中的计时器】\n" + "\n".join(lines)
        return SegmentResult(messages=[{"role": "system", "content": text}])
