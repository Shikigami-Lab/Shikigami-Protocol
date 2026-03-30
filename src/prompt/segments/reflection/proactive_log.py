"""reflection_proactive_log — Recent proactive speech log for deduplication.

Priority 65, inject_into="reflection".

Reads the last 10 proactive speech entries from ase_state.json and injects
them into the reflection prompt so the AI can avoid repeating the same topics.
Consecutive silence_concern entries also trigger a suppression hint.
"""
import os
import time
from typing import List, Dict, Any

from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_locale


_REASON_LABELS_ZH = {
    "memory_recall":      "回忆往事",
    "trend_share":        "分享热点",
    "emotional_overflow": "情绪流露",
    "silence_concern":    "确认沉默",
    "none":               "无特别理由",
}
_REASON_LABELS_EN = {
    "memory_recall":      "memory recall",
    "trend_share":        "trend share",
    "emotional_overflow": "emotional overflow",
    "silence_concern":    "silence concern",
    "none":               "no reason",
}


def _format_ago(seconds: float, locale: str) -> str:
    if seconds < 3600:
        m = int(seconds / 60)
        return f"{m}分钟前" if locale != "en" else f"{m}m ago"
    if seconds < 86400:
        h = int(seconds / 3600)
        return f"{h}小时前" if locale != "en" else f"{h}h ago"
    d = int(seconds / 86400)
    return f"{d}天前" if locale != "en" else f"{d}d ago"


@register
class ReflectionProactiveLogSegment(PromptSegment):
    segment_id = "reflection_proactive_log"
    inject_into = "reflection"
    is_readonly = True
    is_core = False
    default_enabled = True
    priority = 65
    label = "主动发言去重"
    description = "检测近期主动话题，避免反复说同类内容"

    def build(self, ctx) -> SegmentResult:
        if not isinstance(ctx, ReflectionBuildContext):
            return SegmentResult(messages=[])

        locale = ctx.locale or get_locale()

        # Find ase_state.json path via profile storage root
        storage_root = os.path.join("profiles", ctx.profile_id)
        ase_state_path = os.path.join(storage_root, "ase_state.json")
        if not os.path.exists(ase_state_path):
            return SegmentResult(messages=[])

        try:
            import json
            with open(ase_state_path, "r", encoding="utf-8") as f:
                ase_state = json.load(f)
        except Exception:
            return SegmentResult(messages=[])

        log: List[Dict[str, Any]] = ase_state.get("proactive_log", [])
        if not log:
            return SegmentResult(messages=[])

        now = time.time()
        reason_labels = _REASON_LABELS_EN if locale == "en" else _REASON_LABELS_ZH

        if locale == "en":
            header = "[Your Recent Proactive Topics]"
            footer_tpl = "You've initiated {n} times with silence_concern in a row. If motivation is the same again, lower urgency to 0.0~0.2."
            hint_label = "Note"
        else:
            header = "【你最近主动提起的话题】"
            footer_tpl = "你已连续 {n} 次以 silence_concern 为动机主动发言。若本次动机相同，请将 urgency 压低至 0.0~0.2。"
            hint_label = "注意"

        lines = [header]
        for entry in log[-10:]:
            ts = entry.get("timestamp", 0)
            reason = entry.get("speak_reason", entry.get("reason", "none"))
            topic = entry.get("topic_hint", "")
            ago = _format_ago(now - ts, locale) if ts else "?"
            rlabel = reason_labels.get(reason, reason)
            if locale == "en":
                lines.append(f"- [{ago}] {rlabel}: \"{topic}\"")
            else:
                lines.append(f"- [{ago}] {rlabel}：「{topic}」")

        # Count consecutive silence_concern from the tail
        consec_sc = 0
        for entry in reversed(log):
            reason = entry.get("speak_reason", entry.get("reason", "none"))
            if reason == "silence_concern":
                consec_sc += 1
            else:
                break

        if consec_sc >= 2:
            lines.append("")
            lines.append(footer_tpl.format(n=consec_sc))

        content = "\n".join(lines)
        return SegmentResult(messages=[{"role": "system", "content": content}])
