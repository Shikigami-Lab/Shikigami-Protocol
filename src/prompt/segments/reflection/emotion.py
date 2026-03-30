"""reflection_emotion — Current emotion state for reflection.

Formats the current emotion layers (or primary/secondary emotions) into
a human-readable summary for injection into the reflection prompt.
"""
from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_locale, get_prompt


@register
class ReflectionEmotionSegment(PromptSegment):
    segment_id = "reflection_emotion"
    inject_into = "reflection"
    is_readonly = True
    is_core = False
    priority = 30
    label = "当前情感"
    description = "向自省引擎注入当前情绪状态（emotion_state.json）"

    def build(self, ctx) -> SegmentResult:  # ctx: ReflectionBuildContext
        if not isinstance(ctx, ReflectionBuildContext):
            return SegmentResult(messages=[])
        emotion = ctx.emotion
        if not emotion:
            return SegmentResult(messages=[])
        locale = ctx.locale or get_locale()
        emotion_header = get_prompt("reflection.emotion_label", locale=locale,
                                    default=("[Current Emotion]" if locale == "en" else "【当前情感】"))
        layers = emotion.get("emotion_layers") or []
        if layers:
            parts = []
            for lay in layers[:3]:
                e = lay.get("emotion", "")
                i = lay.get("intensity", 0.5)
                if e:
                    parts.append(f"{e}({int(i * 100)}%)")
            if parts:
                content = emotion_header + ", ".join(parts)
                return SegmentResult(messages=[{"role": "system", "content": content}])
        else:
            primary = emotion.get("primary_emotion", "")
            weight = emotion.get("primary_weight", 0.5)
            if primary:
                if locale == "en":
                    content = f"{emotion_header} {primary} ({int(weight * 100)}%)"
                else:
                    content = f"{emotion_header}{primary}（{int(weight * 100)}%）"
                return SegmentResult(messages=[{"role": "system", "content": content}])
        return SegmentResult(messages=[])
