"""ase_behavioral_guidance — Core behavioral rules for ASE proactive speech.

Tells the AI how to behave when initiating: keep it short, don't ask if
they're away, say (silence) if nothing to say, etc.

Injected into the system message so it acts as a persistent instruction.
"""
from src.prompt.base import PromptSegment, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_prompt, get_locale


@register
class AseBehavioralGuidanceSegment(PromptSegment):
    segment_id = "ase_behavioral_guidance"
    inject_into = "ase"
    is_readonly = False
    is_core = True
    priority = 85
    label = "主动发言行为规则"
    label_en = "Proactive Speech Rules"
    description = "控制主动发言的基本规则：简短自然、不问对方是否不在、无话可说时可回复（静默）"
    description_en = "Core rules for proactive speech: keep it brief, don't ask if they're away, reply (silence) if nothing to say"
    default_trigger_mode = "always"

    def build(self, ctx) -> SegmentResult:
        locale = get_locale()
        content = get_prompt("ase.behavioral_guidance", locale=locale,
                             default=(
                                 "[You are initiating proactively, not answering a question]\n"
                                 "- Say what you want to say directly; avoid openings like \"Is there anything I can help with?\" or \"I want to ask you...\"\n"
                                 "- Keep it short and natural; stop when it's enough — no long monologues\n"
                                 "- Don't bring up themes like \"you're not here\" / \"you stopped talking\"\n"
                                 "- If there's truly nothing worth saying, you may reply with \"(silence)\""
                                 if locale == "en"
                                 else "【此刻你是主动开口，不是在回应对方的提问】\n"
                                      "- 直接说你想说的，不要以「有什么我能帮你的吗」或「我想问你……」之类的开头\n"
                                      "- 简短、自然，说完就够，不需要展开成长篇\n"
                                      "- 不要主动提起对方「不在」「不说话了」之类的话题\n"
                                      "- 如果此刻真的没有值得说的话，可以只回复「（静默）」"
                             ))
        return SegmentResult(messages=[{"role": "system", "content": content}])
