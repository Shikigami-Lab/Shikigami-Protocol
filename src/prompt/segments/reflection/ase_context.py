"""reflection_ase_context — Last ASE proactive speech context for reflection.

Only injects content when the user has NOT replied since the last ASE
proactive message, so the reflection LLM knows the AI is waiting for a reply.
"""
from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_locale, get_prompt, render


@register
class ReflectionAseContextSegment(PromptSegment):
    segment_id = "reflection_ase_context"
    inject_into = "reflection"
    is_readonly = True
    is_core = False
    priority = 60
    label = "上次ASE发言"
    description = "向自省引擎注入上次主动发言内容（仅当用户尚未回复时）"

    def build(self, ctx) -> SegmentResult:  # ctx: ReflectionBuildContext
        if not isinstance(ctx, ReflectionBuildContext):
            return SegmentResult(messages=[])
        ase_context = ctx.ase_context
        if ase_context is None:
            return SegmentResult(messages=[])
        locale = ctx.locale or get_locale()
        last_speak_time = ase_context.get("last_speak_time")
        last_user_message_time = ctx.last_user_message_time
        user_replied_since_last_ase = (
            last_user_message_time is not None
            and last_speak_time is not None
            and last_user_message_time > last_speak_time
        )
        parts = []
        content = (ase_context.get("last_speak_content") or "").strip()
        if content and not user_replied_since_last_ase:
            excerpt = content[:150] + ("…" if len(content) > 150 else "")
            parts.append(render("reflection.last_spoke_label", locale=locale,
                                default=("You last initiated with: \"$excerpt\""
                                         if locale == "en"
                                         else "你上次主动说的是：「$excerpt」"), excerpt=excerpt))
            secs = ase_context.get("seconds_since_last_speak")
            if secs is not None:
                parts.append(render("reflection.ase_speak_ago", locale=locale,
                                    default=("[Your Proactive Speech] $minutes minutes since you last initiated"
                                             if locale == "en"
                                             else "【你的主动发言】距离你上次主动开口已经 $minutes 分钟"),
                                    minutes=int(secs / 60)))
            else:
                parts.append(get_prompt("reflection.ase_never_spoke", locale=locale,
                                        default=("[Your Proactive Speech] You have not yet initiated"
                                                 if locale == "en"
                                                 else "【你的主动发言】你尚未主动说过话")))
            parts.append(get_prompt("reflection.ase_user_no_reply", locale=locale,
                                    default=("Your last message was a proactive one; the user has not yet replied."
                                             if locale == "en"
                                             else "上一条消息是你主动说的，用户尚未回复。")))
        consec = ase_context.get("consecutive_speaks", 0)
        if consec and int(consec) > 0:
            parts.append(render("reflection.ase_consec_speaks", locale=locale,
                                default=("You have initiated $consec consecutive times without a reply;"
                                         if locale == "en"
                                         else "你已连续主动发言 $consec 次，用户均未回复；"),
                                consec=int(consec)))
        if not parts:
            return SegmentResult(messages=[])
        return SegmentResult(messages=[{"role": "system", "content": "\n\n".join(parts)}])
