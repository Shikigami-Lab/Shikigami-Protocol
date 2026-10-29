"""Pure wire normalization with in-process provenance for history timestamps.

PromptMessage attributes are deliberately NOT dictionary keys: JSON previews/logs
cannot serialize them. Only ordinary dictionaries leave normalize_messages.
"""
from typing import Optional, Tuple


class PromptMessage(dict):
    """A message with private, non-serialized prompt-building information."""

    def __init__(self, message, *, time_sources=(), time_index_span=None):
        super().__init__(message)
        self.time_sources: Tuple[str, ...] = tuple(time_sources)
        # Exact character range reserved by the pipeline after macro expansion.
        # Replacing only this range preserves later appended tool context without
        # parsing system text or trying to recover timestamps from it.
        self.time_index_span: Optional[Tuple[int, int]] = time_index_span


def _history_time_index(messages):
    lines = [
        "【历史消息时间索引】",
        "编号对应发送前归一化后的全部非 system 消息（从 1 开始）；"
        "片段按合并前的消息顺序列出，不按正文段落拆分。",
    ]
    for index, message in enumerate(messages, start=1):
        sources = "；".join(f"片段{i}：{source}"
                           for i, source in enumerate(message.time_sources, start=1))
        lines.append(f"{index}. {message['role']}：{sources}")
    lines.append("时间仅用于理解消息发生时间和间隔，不是对话发言；不要照抄索引格式。")
    return "\n".join(lines)


def normalize_messages(messages):
    """Merge same-role turns, add a leading user if needed, then render times.

    Pure and idempotent on the resulting wire payload. Pipeline callers may
    still pop/replace/append raw turns; source attributes survive until this
    final boundary. Untagged additions never inherit a historical timestamp.
    """
    systems, conversation = [], []
    for message in messages:
        copy = PromptMessage(
            message,
            time_sources=getattr(message, "time_sources", ()) or
                         ("非历史消息/当前触发（时间未知）",),
            time_index_span=getattr(message, "time_index_span", None),
        )
        if copy["role"] == "system":
            systems.append(copy)
        elif conversation and copy["role"] == conversation[-1]["role"]:
            previous = conversation[-1]
            previous["content"] = (previous["content"] or "") + "\n\n" + (copy["content"] or "")
            previous.time_sources += copy.time_sources
        else:
            conversation.append(copy)
    if conversation and conversation[0]["role"] != "user":
        conversation.insert(0, PromptMessage(
            {"role": "user", "content": "（对话开始）"},
            time_sources=("对话开始占位（非历史消息，无时间）",),
        ))
    for message in systems:
        if message.time_index_span is not None:
            start, end = message.time_index_span
            content = message["content"]
            message["content"] = content[:start] + _history_time_index(conversation) + content[end:]
    return [dict(message) for message in systems + conversation]
