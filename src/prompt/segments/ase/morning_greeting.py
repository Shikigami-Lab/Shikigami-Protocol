"""ase_morning_greeting — Time-window greeting hint for ASE proactive speech.

Injected into the ASE extra_system when the current hour falls within
the configured time window (default 5–10 AM).  Content is user-editable.
"""
from src.prompt.base import PromptSegment, SegmentResult
from src.prompt.registry import register


@register
class AseMorningGreetingSegment(PromptSegment):
    segment_id = "ase_morning_greeting"
    inject_into = "ase"
    is_readonly = False          # content editable by user
    is_core = False
    priority = 50
    label = "时段问候"
    label_en = "Time-Window Greeting"
    description = "在「触发方式」设定的时段内注入（如 5–10 点早晨、18–22 点晚上）。"
    description_en = "Injects a greeting hint during the configured time window (e.g. 5–10 AM morning, 18–22 evening)"
    default_trigger_mode = "time_window"
    default_trigger_param = 510.0  # 5–10 AM  (start*100+end)

    DEFAULT_CONTENT = "若自然，可简单问好（如早上好）。"

    def build(self, ctx) -> SegmentResult:
        # For ASE segments, the engine reads content from SegmentMeta.content
        # (falling back to DEFAULT_CONTENT) and evaluates triggers independently.
        # This build() is only called if an ASE segment is built via the segment
        # system directly; the content returned here is the default.
        return SegmentResult(messages=[{"role": "system", "content": self.DEFAULT_CONTENT}])
