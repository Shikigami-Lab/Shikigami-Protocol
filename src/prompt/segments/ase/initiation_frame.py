"""ase_initiation_frame — Sets "initiating" posture before persona is loaded.

Priority -1 ensures this appears first in the ASE system message, before
the persona segment (priority 0), framing the entire interaction as
"I am choosing to speak" rather than "I am responding to a user".
"""
from src.prompt.base import PromptSegment, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_prompt, get_locale


@register
class AseInitiationFrameSegment(PromptSegment):
    segment_id = "ase_initiation_frame"
    inject_into = "ase"
    is_readonly = True
    is_core = True
    priority = -1
    label = "主动开口姿态"
    description = '在 persona 之前注入姿态重置，建立"主动开口"而非"回应"的基准框架'
    default_trigger_mode = "always"

    def build(self, ctx) -> SegmentResult:
        locale = get_locale()
        content = get_prompt("ase.initiation_frame", locale=locale,
                             default=(
                                 "You are initiating, not responding. You chose this moment. Say less, leave room."
                                 if locale == "en"
                                 else "此刻是你主动开口，不是在回应。你选择了这个时机，你有想说的。说短，留空间。"
                             ))
        return SegmentResult(messages=[{"role": "system", "content": content}])
