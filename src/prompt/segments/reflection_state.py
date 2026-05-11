"""reflection_state.py — Prompt segment for injecting ReflectionEngine output.

Priority 75 — fires after time_context (70), before affinity_state (80).

Injects the AI's current inner state into every normal chat so the
model's tone and attitude are shaped by its reflection output.
Injects both thought (first-person inner monologue) and style_hint
(third-person behavioral description).

When chat_inject_topic_anchor is enabled (per-profile reflection_config), also
injects topic_anchor as a weak hint.

Format injected (system block):
    [此刻内心]
    {thought}
    [当前行为倾向]
    {style_hint}
    （此刻的情绪自然流露在言行中，无需直接提及。）
    [若启用] 最近话题：{topic_anchor}。（可自然提及，不必硬接。）
"""
import logging
import time
from string import Template

from src.config.prompt_loader import get_prompt, get_locale, render
from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)


def _profile_reflection_config(profile_id: str) -> dict:
    """Per-profile reflection_config (e.g. chat_inject_topic_anchor, long_absence_hours)."""
    try:
        from src.config.effective_config import _load_profile_card
        card = _load_profile_card(profile_id)
        return card.get("reflection_config") or {}
    except Exception:
        return {}


@register
class ReflectionStateSegment(PromptSegment):
    segment_id = "reflection_state"
    priority = 75
    label = "当前内心状态"
    description = "将自省引擎最近一次的内心独白注入到 prompt 中"
    is_core = False
    default_trigger_mode = "always"

    def build(self, ctx: BuildContext) -> SegmentResult:
        session = ctx.session
        app = ctx.extras.get("app")

        # Check reflection is enabled in config
        if app:
            cfg = app.state.config.get_reflection_config()
            if not cfg.get("enabled"):
                return SegmentResult(fired=False)
            ttl = cfg.get("state_ttl_seconds", 600)
        else:
            ttl = 600

        state = getattr(session, "reflection_state", None)
        if not state:
            return SegmentResult(fired=False)

        # Skip if state is stale
        age = time.time() - state.get("updated_at", 0)
        if age > ttl:
            return SegmentResult(fired=False)

        thought = state.get("thought", "").strip()
        style_hint = state.get("style_hint", "").strip()
        topic_anchor = state.get("topic_anchor", "").strip()
        speak_reason = state.get("speak_reason", "").strip()
        profile_ref = _profile_reflection_config(getattr(session, "profile_id", "") or "")
        chat_inject_topic = profile_ref.get("chat_inject_topic_anchor", profile_ref.get("chat_inject_topic_hint", True))

        locale = get_locale()
        parts = []
        if thought:
            parts.append(render("reflection.thought_segment", locale=locale,
                                default=(
                                    "[Inner State Right Now]\n$thought"
                                    if locale == "en"
                                    else "[此刻内心]\n$thought"
                                ),
                                thought=thought))
        if style_hint:
            parts.append(render("reflection.style_hint_segment", locale=locale,
                                default=(
                                    "[Current Behavioral Tendency]\n$style_hint\n"
                                    "(Let this emotional state naturally come through in speech and action — no need to mention it directly.)"
                                    if locale == "en"
                                    else "[当前行为倾向]\n$style_hint\n（此刻的情绪自然流露在言行中，无需直接提及。）"
                                ),
                                style_hint=style_hint))
        if chat_inject_topic and topic_anchor:
            parts.append(render("reflection.topic_anchor_segment", locale=locale,
                                default=(
                                    "Recent topic: $topic_anchor. (May be naturally mentioned — no need to force it.)"
                                    if locale == "en"
                                    else "最近话题：$topic_anchor。（可自然提及，不必硬接。）"
                                ),
                                topic_anchor=topic_anchor))

        # Inject speak_reason motivation hint (relevant when ASE fires proactively)
        if speak_reason and speak_reason != "none":
            reason_key = f"reflection.speak_reason_{speak_reason}"
            reason_text = render(reason_key, locale=locale, default="")
            if reason_text:
                parts.append(reason_text)

        if not parts:
            return SegmentResult(fired=False)

        content = "\n\n".join(parts)
        return SegmentResult(messages=[{"role": "system", "content": content}])
