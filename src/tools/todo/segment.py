"""TodosSegment — injects pending todos into LLM context (cooldown-triggered)."""
import logging

from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register
from src.tools.todo.store import TodoStore

logger = logging.getLogger(__name__)


@register
class TodosSegment(PromptSegment):
    segment_id = "todos"
    label = "待办事项"
    description = "定期注入当前待完成事项，每5小时提醒一次（Cooldown触发，UI可调）"
    priority = 50
    default_trigger_mode = "cooldown"
    default_trigger_param = 300.0   # 5小时 = 300分钟

    def build(self, ctx: BuildContext) -> SegmentResult:
        try:
            todos = TodoStore(ctx.session.storage_root).get_pending()
        except Exception as e:
            logger.warning("[TodosSegment] load failed: %s", e)
            return SegmentResult(messages=[])

        if not todos:
            return SegmentResult(messages=[])   # 无待办 → 不注入，mark_fired 不触发

        lines = [f"{i+1}. {t.content}" for i, t in enumerate(todos[:10])]
        text = "【待办事项】当前有 %d 件待完成事项：\n%s" % (len(todos), "\n".join(lines))
        return SegmentResult(messages=[{"role": "system", "content": text}])
