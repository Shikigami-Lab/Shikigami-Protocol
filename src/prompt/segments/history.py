"""History segment — injects recent conversation turns into the message list.

Priority=90 is the sentinel that tells the pipeline this segment emits
user/assistant messages rather than a system block.

Single chat (merge): uses get_merged_recent_messages when
group_chat_merge_into_history is enabled; otherwise single store only.

Group chat: when extras contain current_group_gname and merge is enabled,
uses the same merged timeline (single + all groups) as single chat; only the
current group's other AIs are shown as "[sender]: content". Otherwise falls
back to group store only + [sender]: content for other AIs.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Dict, List

from src.llm.message_normalization import PromptMessage
from src.memory.merged_history import get_merged_recent_messages
from src.prompt.base import HISTORY_PRIORITY, BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)


def _format_timestamp(timestamp: Any) -> str:
    try:
        return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _single_provenance(merged, store, n_turns):
    """Retain single-chat metadata across the legacy merged-history projection.

    Keep the merger's ordering, caps and speech. Its group records lack a stable
    store ID, so those need metadata forwarding in merged_history itself. Do not
    infer status from speech or match across stores. Once the merger forwards
    metadata this compatibility path is a no-op.
    """
    if not any(m.get("source", "single") == "single" and "generation_status" not in m
               for m in merged):
        return merged

    def key(m):
        return (m.get("timestamp"), m.get("role"), (m.get("content") or "").strip(),
                (m.get("sender") or "").strip())

    sources = defaultdict(deque)
    for m in store.get_recent_with_ts(n_turns):
        sources[key(m)].append(m)
    result = []
    # Newest-first matching handles a cap cutting through identical timestamped
    # rows (the store and merger both retain chronological insertion order).
    for m in reversed(merged):
        item = dict(m)
        if item.get("source", "single") == "single" and sources[key(item)]:
            original = sources[key(item)].pop()
            for field in ("id", "agent_run_id", "generation_status"):
                if field in original:
                    item.setdefault(field, original[field])
        result.append(item)
    return list(reversed(result))


def _exclude_current_user_message(
    formatted: List[Dict[str, str]],
    source: List[Dict[str, Any]],
    current_user_message: str,
):
    current_user_message = current_user_message or ""
    if not formatted or not source or not current_user_message.strip():
        return formatted, source
    last_source = source[-1]
    if (last_source.get("role") == "user"
            and (last_source.get("content") or "").strip() == current_user_message.strip()):
        return formatted[:-1], source[:-1]
    return formatted, source


def _result_with_time_sources(
    formatted: List[Dict[str, str]],
    source: List[Dict[str, Any]],
    current_user_message: str,
) -> SegmentResult:
    formatted, source = _exclude_current_user_message(formatted, source, current_user_message)
    # Reserve the index's system priority, but defer numbering until the final
    # wire structure is known. Attributes never enter the store or JSON payload.
    messages = ([PromptMessage({"role": "system", "content": ""},
                               time_index_span=(0, 0))] if source else [])
    for message, original in zip(formatted, source):
        metadata = "历史 " + (_format_timestamp(original.get("timestamp")) or "时间未知")
        status = original.get("generation_status", "completed")
        if original.get("role") == "assistant" and status in ("cancelled", "failed"):
            # Fixed, trusted metadata attached to this fragment survives role
            # conversion/merging and is rendered only in the system time index.
            metadata += (f"；AI 回复未完成（generation_status={status}），仅为部分输出，"
                         "不代表任务或工具执行成功；不可据此重复有副作用的操作")
        messages.append(PromptMessage(message, time_sources=(metadata,)))
    return SegmentResult(messages=messages)


def _transform_for_group_chat(
    msgs: list,
    reply_as_sender: str,
    user_name: str,
) -> list:
    """Convert history for one AI in group chat: other AIs' messages → user with name prefix."""
    out = []
    for m in msgs:
        role = m.get("role") or "user"
        content = m.get("content") or ""
        sender = (m.get("sender") or "").strip()
        if role == "user":
            out.append({"role": "user", "content": content})
        elif role == "assistant":
            if sender == reply_as_sender:
                out.append({"role": "assistant", "content": content})
            else:
                label = sender or "AI"
                out.append({"role": "user", "content": f"[{label}]: {content}"})
        else:
            out.append({"role": role, "content": content})
    return out


def _format_merged_for_llm(merged: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """将 get_merged_recent_messages 的返回格式化为 LLM 需要的 {role, content}，群消息加【群「gname」】sender: 前缀。"""
    out: List[Dict[str, str]] = []
    for m in merged:
        role = m.get("role") or "user"
        content = (m.get("content") or "").strip()
        if m.get("source") == "group":
            content = f"【群「{m.get('gname', '')}」】{m.get('sender', '')}: {content}"
        out.append({"role": role, "content": content})
    return out


def _format_merged_for_group_chat(
    merged: List[Dict[str, Any]],
    reply_as_sender: str,
    user_name: str,
    current_group_gname: str,
) -> List[Dict[str, str]]:
    """群聊时用合并时间线：当前群内其他 AI 的 assistant 转为 [sender]: content；单聊/其他群保持与单聊一致的格式。"""
    out: List[Dict[str, str]] = []
    for m in merged:
        role = m.get("role") or "user"
        content = (m.get("content") or "").strip()
        source = m.get("source") or "single"
        gname = (m.get("gname") or "").strip()
        sender = (m.get("sender") or "").strip()
        if source == "group" and gname == current_group_gname:
            if role == "user":
                out.append({"role": "user", "content": content})
            elif role == "assistant":
                if sender == reply_as_sender:
                    out.append({"role": "assistant", "content": content})
                else:
                    label = sender or "AI"
                    out.append({"role": "user", "content": f"[{label}]: {content}"})
            else:
                out.append({"role": role, "content": content})
        else:
            if source == "group":
                content = f"【群「{gname}」】{sender}: {content}"
            out.append({"role": role, "content": content})
    return out


@register
class HistorySegment(PromptSegment):
    segment_id  = "history"
    priority    = HISTORY_PRIORITY   # 90 — pipeline splits here
    label       = "对话历史"
    description = "注入最近 N 轮对话历史（user/assistant 消息对）；单聊时可合并群聊近期"
    is_core     = True

    default_trigger_mode = "always"

    def build(self, ctx: BuildContext) -> SegmentResult:
        extras = ctx.extras or {}
        reply_as = extras.get("group_reply_as_sender")
        user_name = extras.get("group_user_name") or "用户"
        current_group_gname = extras.get("current_group_gname")
        app = extras.get("app")
        merge_enabled = (ctx.profile or {}).get("memory_config", {}).get("group_chat_merge_into_history") is not False
        has_group_manager = bool(getattr(getattr(app, "state", None), "group_manager", None) if app else None)

        if reply_as is not None and reply_as != "":
            if current_group_gname is not None and merge_enabled and has_group_manager:
                merged = get_merged_recent_messages(
                    ctx.session,
                    app,
                    ctx.profile,
                    ctx.n_history_turns,
                    cap=None,
                )
                merged = _single_provenance(merged, ctx.session.conversation_store, ctx.n_history_turns)
                msgs = _format_merged_for_group_chat(merged, reply_as, user_name, current_group_gname)
                return _result_with_time_sources(msgs, merged, ctx.user_msg)
            msgs = ctx.store.get_recent_with_ts(ctx.n_history_turns)
            formatted = _transform_for_group_chat(msgs, reply_as, user_name)
            return _result_with_time_sources(formatted, msgs, ctx.user_msg)
        merged = get_merged_recent_messages(
            ctx.session,
            app,
            ctx.profile,
            ctx.n_history_turns,
            cap=None,
        )
        merged = _single_provenance(merged, ctx.session.conversation_store, ctx.n_history_turns)
        formatted = _format_merged_for_llm(merged)
        return _result_with_time_sources(formatted, merged, ctx.user_msg)
