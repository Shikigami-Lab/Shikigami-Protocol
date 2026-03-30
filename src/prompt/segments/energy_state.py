"""energy_state segment — 注入当前能量数值及对应的语言风格建议。

能量档位的**文案**以 `config/prompts/emotion.yaml` 的 `energy_prompts` 为唯一默认来源；
本文件只保留「energy_level 数值区间 → 档位键」的映射（与 YAML 中 "0"/"10"/… 键一致）。
人格卡 `emotion_config.energy_prompts` / `energy_prompts` 可按键覆盖，未写的键仍回落到 YAML。
"""
import json
import logging
import os

from src.config.prompt_loader import get_dict, get_locale
from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)

# (lo, hi, yaml_key) — 与 emotion.yaml 中 energy_prompts 的键一致；先匹配高能量档
_ENERGY_BANDS = [
    (80, 100, "80"),
    (60, 80, "60"),
    (30, 60, "30"),
    (10, 30, "10"),
    (0, 10, "0"),
]


def _resolve_energy_value(v, locale: str) -> str:
    """支持 profile 里写字符串，或 {zh, en} 双语叶节点（与 YAML 一致）。"""
    if v is None:
        return ""
    if isinstance(v, dict) and ("zh" in v or "en" in v):
        return (v.get(locale) or v.get("zh") or "").strip()
    return str(v).strip()


@register
class EnergyStateSegment(PromptSegment):
    segment_id = "energy_state"
    priority = 66
    label = "能量状态"
    description = "注入当前能量数值及对应的语言风格建议"
    is_core = False
    default_trigger_mode = "always"
    default_trigger_param = 1.0

    def build(self, ctx: BuildContext) -> SegmentResult:
        state_path = os.path.join(ctx.session.storage_root, "emotion_state.json")
        if not os.path.exists(state_path):
            return SegmentResult(fired=False)

        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            logger.warning("[energy_state] failed to load state: %s", e)
            return SegmentResult(fired=False)

        energy = float(state.get("energy_level", 80.0))
        locale = get_locale()

        # 默认：YAML emotion.energy_prompts（已按 locale 解析）
        yaml_prompts = get_dict("energy_prompts", locale=locale)
        if not isinstance(yaml_prompts, dict):
            yaml_prompts = {}

        profile_raw = (
            (ctx.profile.get("emotion_config") or {}).get("energy_prompts")
            or ctx.profile.get("energy_prompts")
        )
        effective = dict(yaml_prompts)
        if isinstance(profile_raw, dict) and profile_raw:
            for k, v in profile_raw.items():
                resolved = _resolve_energy_value(v, locale)
                if resolved:
                    effective[str(k)] = resolved

        content_body = ""
        band_key = "30"
        for lo, hi, key in _ENERGY_BANDS:
            if lo <= energy <= hi:
                band_key = key
                content_body = (effective.get(key) or "").strip()
                break

        if not content_body:
            logger.warning(
                "[energy_state] missing energy_prompts[%s] (locale=%s); check config/prompts/emotion.yaml",
                band_key,
                locale,
            )
            content_body = (
                "[Energy state prompt missing for this band; check emotion.yaml energy_prompts.]"
                if locale == "en"
                else "【能量状态】对应档位文案缺失，请检查 config/prompts/emotion.yaml 中的 energy_prompts。"
            )

        return SegmentResult(messages=[{"role": "system", "content": content_body}])
