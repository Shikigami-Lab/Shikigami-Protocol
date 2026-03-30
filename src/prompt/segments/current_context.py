"""当前场景 segment — 单聊且启用合并时注入「当前为与用户的一对一私聊」说明，避免模型混淆单聊/群聊。"""
from __future__ import annotations

from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

# 放在 history(90) 之前、其他 system 之后，作为最后一段 system 说明
CURRENT_CONTEXT_PRIORITY = 89


@register
class CurrentContextSegment(PromptSegment):
    segment_id = "current_context"
    priority = CURRENT_CONTEXT_PRIORITY
    label = "当前场景"
    description = "单聊且合并群聊历史时，注明当前为一对一私聊，群聊记录仅作背景参考。"
    is_core = False

    default_trigger_mode = "always"

    def build(self, ctx: BuildContext) -> SegmentResult:
        reply_as = (ctx.extras or {}).get("group_reply_as_sender")
        if reply_as is not None and reply_as != "":
            return SegmentResult(messages=[])
        mem = (ctx.profile.get("memory_config") or {})
        if mem.get("group_chat_merge_into_history") is False:
            return SegmentResult(messages=[])
        content = (
            "当前是与用户的一对一私聊。以下对话历史中标注【群「…」】的为群聊中的记录，仅作背景参考；"
            "请以私聊身份只对用户回复，不要当作在群聊中发言。"
        )
        return SegmentResult(messages=[{"role": "system", "content": content}])
