"""reflection_silence — User silence duration for reflection.

Injects a tiered "last spoke" summary so the reflection LLM can factor
in how long the user has been away.
"""
from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_locale, render


@register
class ReflectionSilenceSegment(PromptSegment):
    segment_id = "reflection_silence"
    inject_into = "reflection"
    is_readonly = True
    is_core = True
    priority = 50
    label = "用户沉默时长"
    description = "向自省引擎注入用户最近说话的时间（分级描述）"

    def build(self, ctx) -> SegmentResult:  # ctx: ReflectionBuildContext
        if not isinstance(ctx, ReflectionBuildContext):
            return SegmentResult(messages=[])
        locale = ctx.locale or get_locale()
        silence_text = _format_silence_tier(ctx.silent_seconds, locale)
        content = render("reflection.user_time_label", locale=locale,
                         default=("[User Last Spoke] $silence" if locale == "en"
                                  else "【用户说话时间】$silence"),
                         silence=silence_text)
        return SegmentResult(messages=[{"role": "system", "content": content}])


def _format_silence_tier(silent_seconds: float, locale: str = None) -> str:
    """Tiered silence description: within 5 min → just spoke; then minutes/hours/days."""
    from src.config.prompt_loader import get_prompt, get_locale, render as _render
    if locale is None:
        locale = get_locale()
    if silent_seconds < 300:
        return get_prompt("reflection.silence_tier.just_spoke", locale=locale,
                          default=("user just spoke within 5 minutes" if locale == "en"
                                   else "5分钟之内，用户才说过话"))
    if silent_seconds < 3600:
        return _render("reflection.silence_tier.minutes", locale=locale,
                       default=("user has been silent for $minutes minutes" if locale == "en"
                                else "用户已沉默 $minutes 分钟"),
                       minutes=int(silent_seconds / 60))
    if silent_seconds < 86400:
        return _render("reflection.silence_tier.hours", locale=locale,
                       default=("user has been silent for $hours hours" if locale == "en"
                                else "用户已沉默 $hours 小时"),
                       hours=int(silent_seconds / 3600))
    return _render("reflection.silence_tier.days", locale=locale,
                   default=("user has been silent for $days days" if locale == "en"
                            else "用户已沉默 $days 天"),
                   days=int(silent_seconds / 86400))
