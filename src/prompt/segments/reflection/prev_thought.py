"""reflection_prev_thought — Previous reflection result injection.

Injects the last reflection output (thought/urgency/topic_hint/elapsed time)
into the current reflection prompt to provide continuity and help calibrate
urgency changes over time.
"""
import time

from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_locale, get_prompt, render


@register
class ReflectionPrevThoughtSegment(PromptSegment):
    segment_id = "reflection_prev_thought"
    inject_into = "reflection"
    is_readonly = True
    is_core = False
    priority = 20
    label = "上一轮自省"
    description = "上次自省输出（thought/urgency/topic_hint），帮助连贯与校准 urgency"

    def build(self, ctx) -> SegmentResult:  # ctx: ReflectionBuildContext
        if not isinstance(ctx, ReflectionBuildContext):
            return SegmentResult(messages=[])
        prev = ctx.prev_reflection
        if not prev:
            return SegmentResult(messages=[])
        locale = ctx.locale or get_locale()
        urgency = prev.get("urgency")
        topic = (prev.get("topic_hint") or "").strip()
        updated_at = prev.get("updated_at") or 0
        if not topic and urgency is None:
            return SegmentResult(messages=[])

        line = get_prompt("reflection.prev_reflection_header", locale=locale,
                          default=("[Your Last Reflection]" if locale == "en" else "【你上一轮自省】"))
        if topic:
            line += render("reflection.prev_topic", locale=locale,
                           default=("Your last noted topic was: $topic" if locale == "en"
                                    else "你上次在意的话题是：$topic"), topic=topic)
        if urgency is not None:
            line += render("reflection.prev_urgency", locale=locale,
                           default=("; urgency was $urgency" if locale == "en"
                                    else "；urgency 为 $urgency"), urgency=round(urgency, 1))
        if updated_at > 0:
            elapsed = time.time() - updated_at
            if elapsed < 60:
                time_ago = render("reflection.silence_tier.just_spoke", locale=locale,
                                  default=("less than 1 minute" if locale == "en" else "不到1分钟"))
            elif elapsed < 3600:
                time_ago = render("time_context.human_delta.minutes", locale=locale,
                                  default=("$minutes minutes" if locale == "en" else "$minutes 分钟"),
                                  minutes=int(elapsed / 60))
            elif elapsed < 86400:
                time_ago = render("time_context.human_delta.hours", locale=locale,
                                  default=("$hours hours" if locale == "en" else "$hours 小时"),
                                  hours=int(elapsed / 3600))
            else:
                time_ago = render("time_context.human_delta.days", locale=locale,
                                  default=("$days days" if locale == "en" else "$days 天"),
                                  days=int(elapsed / 86400))
            line += render("reflection.prev_elapsed", locale=locale,
                           default=("; $time_ago has passed since your last reflection" if locale == "en"
                                    else "；距离上次自省已经过去 $time_ago"), time_ago=time_ago)
        return SegmentResult(messages=[{"role": "system", "content": line}])
