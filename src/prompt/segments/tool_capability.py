"""Tool capability segments — the former chat.py ``_inject_context`` bypass,
now regular pipeline segments with UI toggles and dependency locking.

Two segments split by intent:

- ``ToolCapabilitySegment`` (priority 89): the "[Tool availability]" capability
  list + timer usage instructions + the task-delegation instruction.  Fires
  only when the caller opts in via ``extras["include_tool_capability"]`` (chat
  endpoint and autonomy runner do; ASE heartbeat does not) and tools are
  actually allowed for this turn.  Locked in the UI when AI tools are off.
- ``ToolGuardSegment`` (priority 89): the "no tools are enabled this turn"
  notice, shown when the caller opted in but this turn cannot use tools
  (tools off, or a conversation-only turn).  Users may disable it; the default
  keeps the model honest about having no tools.

Mid-turn dynamic payloads (task results, command outputs) intentionally stay
out of the segment system — they are produced between two LLM calls of the
same turn and are appended by chat.py with explicit provenance markers.
"""
from __future__ import annotations

import logging
from datetime import datetime

from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)

_NO_TOOLS_NOTICE = ('[Tool availability]\nNo tools are enabled for this turn. '
                    'Chat normally, but do not claim to have executed operations. '
                    'Manual toolbar buttons and slash commands remain available.')


def _opted_in(ctx: BuildContext) -> bool:
    return bool(ctx.extras.get("include_tool_capability"))


def _is_slash(ctx: BuildContext) -> bool:
    return (ctx.user_msg or "").startswith("/")


def _runtime(ctx: BuildContext):
    app = ctx.extras.get("app")
    return getattr(getattr(app, "state", None), "agent_runtime", None)


@register
class ToolCapabilitySegment(PromptSegment):
    segment_id = "tool_capability"
    label = "工具能力与委派指令"
    description = ("告知模型本轮可用的工具、计时器用法与任务委派格式；"
                   "仅在发起方允许工具的轮次注入")
    priority = 89
    is_readonly = True
    requires = ("feature:agent_tools",)

    def build(self, ctx: BuildContext) -> SegmentResult:
        if not _opted_in(ctx) or _is_slash(ctx):
            return SegmentResult(messages=[], fired=False)
        runtime = _runtime(ctx)
        if runtime is None or not runtime.tools_allowed(ctx.session.profile_id):
            return SegmentResult(messages=[], fired=False)
        if ctx.extras.get("conversation_only"):
            return SegmentResult(messages=[], fired=False)

        from src.agent.capabilities import capability_context

        origin = ctx.extras.get("agent_origin") or "user"
        access = runtime.access_for(ctx.session.profile_id)
        text = capability_context(access, origin=origin)

        if origin == "user" and runtime.timer_allowed(ctx.session.profile_id):
            local_now = datetime.now().astimezone().isoformat(timespec='seconds')
            text += (f'\nCurrent host-local time with offset is {local_now}. '
                     'Use timer_create for a countdown with a duration. '
                     'Delegate a one-time reminder for a specific date/time to timer_schedule '
                     'with due_at as an ISO date and time and a label. Omit the offset for local time. '
                     'Use local time unless the user specifies another zone. Resolve relative dates '
                     'using the local time above. Ask if the intended duration, date, time or zone '
                     'needed for the request is unclear. Use timer_list and timer_cancel for both types. '
                     'For a user-requested daily report or its pause, delegate to timer_daily_report. '
                     'Give the local time as HH:MM and the report instructions on creation.')

        if origin == "user" and not ctx.extras.get("continue_run_id"):
            from src.agent.chat_routing import DISPATCH_INSTRUCTION

            text += "\n\n" + DISPATCH_INSTRUCTION

        return SegmentResult(messages=[{"role": "system", "content": text}])


@register
class ToolGuardSegment(PromptSegment):
    segment_id = "tool_guard"
    label = "无工具提示"
    description = ("本轮不可用工具时提醒模型不要声称执行了操作；"
                   "可在关闭后完全静默（不推荐）")
    priority = 89
    is_readonly = True

    def build(self, ctx: BuildContext) -> SegmentResult:
        if not _opted_in(ctx) or _is_slash(ctx):
            return SegmentResult(messages=[], fired=False)
        runtime = _runtime(ctx)
        tools_this_turn = (runtime is not None
                           and runtime.tools_allowed(ctx.session.profile_id)
                           and not ctx.extras.get("conversation_only"))
        if tools_this_turn:
            return SegmentResult(messages=[], fired=False)
        return SegmentResult(messages=[{"role": "system", "content": _NO_TOOLS_NOTICE}])
