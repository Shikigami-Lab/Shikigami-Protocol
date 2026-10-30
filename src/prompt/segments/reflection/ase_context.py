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
        secs = ase_context.get("seconds_since_last_speak")
        user_replied_since_last_ase = (
            last_user_message_time is not None
            and last_speak_time is not None
            and last_user_message_time > last_speak_time
        )
        # 仅当"上一条是 AI 主动发言、用户尚未回复"时才提供等待语境；
        # 用户已回复 / 从未主动说过 → 本段不注入（沉默时长由 reflection_silence 负责）。
        if last_speak_time is None or secs is None or user_replied_since_last_ase:
            return SegmentResult(messages=[])

        parts = []
        content = (ase_context.get("last_speak_content") or "").strip()
        if content:
            excerpt = content[:150] + ("…" if len(content) > 150 else "")
            parts.append(render("reflection.last_spoke_label", locale=locale,
                                default=("You last initiated with: \"$excerpt\""
                                         if locale == "en"
                                         else "你上次主动说的是：「$excerpt」"), excerpt=excerpt))

        # 按"距上次主动开口"选档；每档自带 intention，随时长升温但不催促。
        delta = _format_delta(secs, locale)
        if secs < 6 * 3600:
            tier_key, show_consec = "reflection.ase_speak_ago_recent", True
        elif secs < 36 * 3600:
            tier_key, show_consec = "reflection.ase_speak_ago_day", False
        elif secs < 5 * 86400:
            tier_key, show_consec = "reflection.ase_speak_ago_days", False
        else:
            tier_key, show_consec = "reflection.ase_speak_ago_long", False
        tier_line = render(tier_key, locale=locale, default="", delta=delta)
        if tier_line:
            parts.append(tier_line)

        # "连续 N 次没回"只在 recent 档出现——过了几小时它就是过去式，不该再压制自省。
        consec = int(ase_context.get("consecutive_speaks", 0) or 0)
        if show_consec and consec > 0:
            parts.append(render("reflection.ase_consec_speaks", locale=locale,
                                default=("You have initiated $consec consecutive times without a reply;"
                                         if locale == "en"
                                         else "你已连续主动发言 $consec 次，用户均未回复；"),
                                consec=consec))

        if not parts:
            return SegmentResult(messages=[])
        return SegmentResult(messages=[{"role": "system", "content": "\n\n".join(parts)}])


def _format_delta(secs: float, locale: str) -> str:
    """无后缀的人类可读时长：分钟 / 小时 / 天。

    48 小时以内用"小时"，避免出现"1 天 / 1 days"这种别扭表达；
    与带"前/ago"后缀的 time_context.human_delta 区分，专供"距…已 $delta"句式。
    """
    if secs < 3600:
        return render("reflection.delta_bare.minutes", locale=locale,
                      default=("$minutes minutes" if locale == "en" else "$minutes 分钟"),
                      minutes=max(1, int(secs / 60)))
    if secs < 48 * 3600:
        return render("reflection.delta_bare.hours", locale=locale,
                      default=("$hours hours" if locale == "en" else "$hours 小时"),
                      hours=int(secs / 3600))
    return render("reflection.delta_bare.days", locale=locale,
                  default=("$days days" if locale == "en" else "$days 天"),
                  days=int(secs / 86400))
