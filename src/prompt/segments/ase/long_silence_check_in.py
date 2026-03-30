"""ase_long_silence_check_in — Long silence check-in hint for ASE proactive speech.

Injected into the ASE extra_system when the user has been silent for
longer than the configured silence threshold (default 60 minutes).
Content is user-editable.
"""
from src.prompt.base import PromptSegment, SegmentResult
from src.prompt.registry import register


@register
class AseLongSilenceCheckInSegment(PromptSegment):
    segment_id = "ase_long_silence_check_in"
    inject_into = "ase"
    is_readonly = False          # content editable by user
    is_core = False
    priority = 60
    label = "长时间沉默时可问在干嘛"
    description = "用户沉默超过「触发方式」设定分钟数时注入；若担心太频繁可在同一段落用「冷却时间」限制。"
    default_trigger_mode = "first_after_silence"
    default_trigger_param = 60.0  # 60 minutes silence

    DEFAULT_CONTENT = "用户已有一段时间未发消息，可以温和地问一句在做什么、是否在忙；不要追问或抱怨。"

    def build(self, ctx) -> SegmentResult:
        # For ASE segments, the engine reads content from SegmentMeta.content
        # (falling back to DEFAULT_CONTENT) and evaluates triggers independently.
        # This build() is only called if an ASE segment is built via the segment
        # system directly; the content returned here is the default.
        return SegmentResult(messages=[{"role": "system", "content": self.DEFAULT_CONTENT}])
