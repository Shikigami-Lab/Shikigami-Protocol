"""reflection_recent_dialogue — Recent conversation turns for reflection.

Formats the last 10 turns of conversation for injection into the
reflection system prompt.
"""
from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_locale, get_prompt


@register
class ReflectionRecentDialogueSegment(PromptSegment):
    segment_id = "reflection_recent_dialogue"
    inject_into = "reflection"
    is_readonly = True
    is_core = True
    priority = 80
    label = "最近对话"
    description = "向自省引擎注入最近 10 条对话（reflection 专用格式）"

    def build(self, ctx) -> SegmentResult:  # ctx: ReflectionBuildContext
        if not isinstance(ctx, ReflectionBuildContext):
            return SegmentResult(messages=[])
        locale = ctx.locale or get_locale()
        user_label = "User" if locale == "en" else "用户"
        user_name = ctx.user_name or user_label
        persona_name = ctx.persona_name or "AI"
        recent_turns = ctx.recent_turns or []

        # max_turns: per-profile override → global reflection_cfg → hard default 8
        max_turns = (
            (ctx.profile.get("reflection_config") or {}).get("recent_dialogue_max_turns")
            or ctx.reflection_cfg.get("recent_dialogue_max_turns")
            or 8
        )
        try:
            max_turns = int(max_turns)
        except (TypeError, ValueError):
            max_turns = 8

        if recent_turns:
            turns = recent_turns[-max_turns:]
            lines = []
            for m in turns:
                sender = (m.get("sender") or "").strip()
                content = (m.get("content") or "")[:300].strip()
                is_human = sender == user_name or (not sender and m.get("role", "user") == "user")
                if is_human:
                    lines.append(f"{user_label}：{content}")
                else:
                    lines.append(f"{persona_name}：{content}")
            dialogue_text = "\n".join(lines)
        else:
            dialogue_text = get_prompt("reflection.no_recent_dialogue", locale=locale,
                                       default=("No recent conversation." if locale == "en"
                                                else "暂无最近对话。"))

        dialogue_header = get_prompt("reflection.recent_dialogue_header", locale=locale,
                                     default=("\n\n[Recent Conversation]\n" if locale == "en"
                                              else "\n\n【最近对话】\n"))
        content = dialogue_header + dialogue_text
        return SegmentResult(messages=[{"role": "system", "content": content}])
