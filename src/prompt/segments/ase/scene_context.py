"""ase_scene_context — Scene/situation context for ASE proactive speech.

target="user" — this segment's content goes into the final user message
(narrator-style scene description) so the AI reads it as "what's happening
now" rather than "what the user said to me".

Includes: style_hint, continuation of last speech, time notes, VLM,
thought, topic_anchor, speak_reason, trend items.
"""
from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_prompt, get_locale, render, get_raw


def _get_time_ctx(hour: int, locale: str) -> str:
    periods = get_raw("ase.time_periods") or {}
    mapping = [
        (range(5, 9), "dawn"), (range(9, 12), "morning"),
        (range(12, 14), "midday"), (range(14, 18), "afternoon"),
        (range(18, 22), "evening"),
    ]
    for hr_range, key in mapping:
        if hour in hr_range:
            entry = periods.get(key, {})
            return entry.get(locale) or entry.get("zh") or key
    entry = periods.get("night", {})
    return entry.get(locale) or entry.get("zh") or ("night" if locale == "en" else "深夜")


@register
class AseSceneContextSegment(PromptSegment):
    segment_id = "ase_scene_context"
    inject_into = "ase"
    is_readonly = True
    is_core = True
    priority = 88          # just before history (90), after behavioral guidance (85)
    label = "场景上下文"
    label_en = "Scene Context"
    description = "将当前内心状态、话题、VLM 画面、发言动机等以旁白形式注入，作为主动发言的情境依据"
    description_en = "Injects current inner state, topic, VLM, speak reason as narrator-style scene context for proactive speech"
    default_trigger_mode = "always"

    def build(self, ctx: BuildContext) -> SegmentResult:
        session = ctx.session
        reflection_state = getattr(session, "reflection_state", None) or {}
        extras = ctx.extras or {}
        locale = get_locale()

        thought = reflection_state.get("thought", "")
        topic_anchor = reflection_state.get("topic_anchor", "")
        style_hint = (reflection_state.get("style_hint") or "").strip()
        speak_reason = reflection_state.get("speak_reason", "none")
        vlm_description = extras.get("ase_vlm_description", "")
        last_ase_content = extras.get("ase_last_content", "")
        greeting_hint = extras.get("ase_greeting_hint", "")
        chosen_topic = extras.get("ase_chosen_topic")   # 主动话题发现：自省选定的话题
        user_name = extras.get("user_name") or ("them" if locale == "en" else "对方")

        from datetime import datetime
        hour = datetime.now().hour
        time_ctx = _get_time_ctx(hour, locale)

        parts = []

        # Style tendency (first, sets the tone)
        if style_hint:
            parts.append(render("ase.style_hint_prefix", locale=locale,
                                default=("Current tendency: $style_hint" if locale == "en"
                                         else "角色此刻倾向：$style_hint"),
                                style_hint=style_hint))

        # Continue from last proactive message
        if last_ase_content and last_ase_content.strip():
            raw = last_ase_content.strip()
            snippet = raw[:120] + ("…" if len(raw) > 120 else "")
            parts.append(render("ase.last_spoke_continuation", locale=locale,
                                default=(
                                    "[Continue Your Last Proactive Message] You last initiated with: \"$snippet\". "
                                    "The user has not replied yet. Continue from it or take a new angle — "
                                    "do not repeat it verbatim, and do not ask whether they are away or unwilling to reply."
                                    if locale == "en"
                                    else "【接续上次主动发言】你上次主动说的是：「$snippet」。用户尚未回复。"
                                         "请接着这句话往下说或换一个角度，不要原样重复，也不要问对方是否不在或不想回复。"
                                ),
                                snippet=snippet))

        # Greeting hint (from ASE segment injections like morning_greeting)
        if greeting_hint and greeting_hint.strip():
            parts.append(greeting_hint.strip())

        # Time note (only late night / early dawn)
        night_labels = {
            get_prompt("ase.time_periods.night", locale=locale,
                       default=("night" if locale == "en" else "深夜")),
            get_prompt("ase.time_periods.dawn", locale=locale,
                       default=("dawn" if locale == "en" else "清晨")),
        }
        if time_ctx in night_labels and not vlm_description:
            parts.append(render("ase.time_note", locale=locale,
                                default=("It's $time_ctx right now." if locale == "en"
                                         else "此刻是$time_ctx。"),
                                time_ctx=time_ctx))

        # Speak reason label
        if speak_reason and speak_reason != "none":
            labels = (get_raw("ase.speak_reason_labels") or {}).get(locale) or \
                     (get_raw("ase.speak_reason_labels") or {}).get("zh") or {}
            reason_label = labels.get(speak_reason, "")
            if reason_label:
                parts.append(render("ase.speak_reason_context", locale=locale,
                                    default=("You feel like speaking because: $reason_label"
                                             if locale == "en"
                                             else "你此刻想开口，是因为：$reason_label"),
                                    reason_label=reason_label))

        # ── Scene context: additive, not mutually exclusive ──────────────
        has_scene_content = False

        # VLM 画面
        if vlm_description:
            has_scene_content = True
            parts.append(render("ase.vlm_notice", locale=locale,
                                default=("You notice on their screen — $vlm_description"
                                         if locale == "en"
                                         else "你注意到对方的屏幕上——$vlm_description"),
                                vlm_description=vlm_description))

        # thought（永远注入，这是 reflection 最核心的产出）
        if thought:
            has_scene_content = True
            if vlm_description:
                parts.append(render("ase.thought_after_vlm", locale=locale,
                                    default=("That makes you think of: $thought"
                                             if locale == "en"
                                             else "这让你想起了：$thought"),
                                    thought=thought))
            else:
                parts.append(render("ase.thought_alone", locale=locale,
                                    default=("You've been keeping in mind: $thought"
                                             if locale == "en"
                                             else "你一直留意着：$thought"),
                                    thought=thought))

        # 主动话题发现：自省选定的话题（已决策块）——「你已经决定要说」，无 opt-out
        if chosen_topic:
            has_scene_content = True
            material = (chosen_topic.get("material") or "").strip()
            angle = (chosen_topic.get("angle") or "").strip()
            framing = (chosen_topic.get("framing_hint") or "").strip()
            if locale == "en":
                block = [f"[What You Want to Say] You've been thinking, and you want to "
                         f"bring this up with {user_name} on your own initiative:"]
                if material:
                    block.append(material)
                if angle:
                    block.append(f"How you plan to lead into it: {angle}")
                if framing:
                    block.append(framing)
            else:
                block = [f"【你想说的事】你刚刚想到，想主动找{user_name}聊这个："]
                if material:
                    block.append(material)
                if angle:
                    block.append(f"你打算这样切入：{angle}")
                if framing:
                    block.append(framing)
            parts.append("\n".join(block))
        elif topic_anchor:
            # 无 chosen_topic 时（兜底路径）仍可用 topic_anchor 作弱提示
            has_scene_content = True
            parts.append(render("ase.topic_alone", locale=locale,
                                default=("You recall a previous conversation about \"$topic_anchor\"."
                                         if locale == "en"
                                         else "你想起了之前关于「$topic_anchor」的对话。"),
                                topic_anchor=topic_anchor))

        # 兜底：什么场景信息都没有时
        if not has_scene_content:
            parts.append(render("ase.silence_generic", locale=locale,
                                default=("It's $time_ctx now, and it's quiet around."
                                         if locale == "en"
                                         else "现在是$time_ctx，四周很静。"),
                                time_ctx=time_ctx))

        # Date hint
        parts.append(get_prompt("ase.date_hint", locale=locale,
                                default=(
                                    "The current date/holiday is shown in the time context above; "
                                    "if it fits naturally, you may mention a brief holiday/date-related line."
                                    if locale == "en"
                                    else "当前日期与节日见上方时间上下文；若自然可带出节日或日期相关的一句。"
                                )))

        if not parts:
            return SegmentResult(fired=False)

        content = "\n\n".join(parts)
        return SegmentResult(
            messages=[{"role": "system", "content": content}],
            target="user",
        )
