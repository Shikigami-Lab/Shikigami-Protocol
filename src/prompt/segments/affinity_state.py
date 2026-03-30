"""affinity_state segment — 注入当前好感度数值与对应的关系风格建议。"""
import json
import logging
import os

from src.config.prompt_loader import get_dict, get_locale, get_prompt, get_raw, render
from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)

# Range boundaries and YAML keys (labels/defaults loaded from YAML at runtime)
_LEVEL_RANGES = [
    (float("-inf"), 0,    "-100"),
    (0,    200,  "0"),
    (200,  400,  "200"),
    (400,  600,  "400"),
    (600,  800,  "600"),
    (800,  1000, "800"),
    (1000, 1200, "1000"),
    (1200, float("inf"), "1200"),
]


def _get_level_label(key: str, locale: str) -> str:
    raw = get_raw("affinity.level_labels") or {}
    entry = raw.get(key, {})
    if isinstance(entry, dict):
        return entry.get(locale) or entry.get("zh") or key
    return str(entry)


@register
class AffinityStateSegment(PromptSegment):
    segment_id = "affinity_state"
    priority = 67
    label = "好感度与关系等级"
    description = "注入当前好感度数值与对应的关系风格建议"
    is_core = False
    default_trigger_mode = "always"
    default_trigger_param = 1.0

    def build(self, ctx: BuildContext) -> SegmentResult:
        state_path = os.path.join(ctx.session.storage_root, "affinity_state.json")
        if not os.path.exists(state_path):
            return SegmentResult(fired=False)

        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            logger.warning("[affinity_state] failed to load state: %s", e)
            return SegmentResult(fired=False)

        locale = get_locale()
        affinity = float(state.get("affinity", 100.0))

        # Profile overrides first; else fall back to YAML affinity_prompts
        profile_prompts = (
            (ctx.profile.get("emotion_config") or {}).get("affinity_prompts")
            or ctx.profile.get("affinity_prompts")
        )
        if not isinstance(profile_prompts, dict) or not profile_prompts:
            profile_prompts = get_dict("affinity.affinity_prompts", locale=locale)

        status = _get_level_label("0", locale)
        content_body = get_prompt("affinity.level_defaults.0", locale=locale,
                                  default=("Warm, professional, not initiating" if locale == "en" else "温和、专业，不主动"))

        for lo, hi, key in _LEVEL_RANGES:
            if lo <= affinity < hi:
                status = _get_level_label(key, locale)
                content_body = profile_prompts.get(
                    key,
                    get_prompt(f"affinity.level_defaults.{key}", locale=locale,
                               default=content_body)
                )
                break

        if locale == "en":
            header = render(
                "affinity.status_header", locale=locale,
                default="[Current relationship stage: $status (affinity $affinity/1200) — strictly follow the behavioral rules below for this stage]",
                status=status, affinity=f"{affinity:.0f}",
            )
        else:
            header = render(
                "affinity.status_header", locale=locale,
                default="【当前关系阶段行为约束：$status（好感度 $affinity/1200）— 本轮回应必须严格遵守以下阶段规则，不得擅自升级亲密度】",
                status=status, affinity=f"{affinity:.0f}",
            )
        content = f"{header}\n{content_body}"
        return SegmentResult(messages=[{"role": "system", "content": content}])
