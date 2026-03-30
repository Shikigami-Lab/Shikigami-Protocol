"""Built-in ASE situational segments (时段问候、长时间沉默问在干嘛).

Used by GET /profiles/{id}/reflection_config (unified segments list) and by AseEngine.
Trigger logic: same as Persona prompt segments (trigger_mode + trigger_param).
"""
from typing import Any, Dict, List

# inject_into for these is always "ase"
# default_trigger_mode / default_trigger_param: 与人格 Prompt 段落一致，可被 segment_overrides 覆盖
BUILTIN_ASE_SEGMENTS: List[Dict[str, Any]] = [
    {
        "segment_id": "ase_morning_greeting",
        "label": "时段问候",
        "description": "在「触发方式」设定的时段内注入（如 5–10 点早晨、18–22 点晚上）。",
        "default_content": "若自然，可简单问好（如早上好）。",
        "inject_into": "ase",
        "is_core": False,
        "default_trigger_mode": "time_window",
        "default_trigger_param": 510.0,  # 5 时–10 时（start*100+end）
    },
    {
        "segment_id": "ase_long_silence_check_in",
        "label": "长时间沉默时可问在干嘛",
        "description": "用户沉默超过「触发方式」设定分钟数时注入；若担心太频繁可在同一段落用「冷却时间」限制。",
        "default_content": "用户已有一段时间未发消息，可以温和地问一句在做什么、是否在忙；不要追问或抱怨。",
        "inject_into": "ase",
        "is_core": False,
        "default_trigger_mode": "first_after_silence",
        "default_trigger_param": 60.0,  # 沉默 60 分钟
    },
]


def get_builtin_ase_segments() -> List[Dict[str, Any]]:
    """Return copy of builtin segment definitions for API/serialization."""
    return [dict(s) for s in BUILTIN_ASE_SEGMENTS]


def get_builtin_segment_ids() -> List[str]:
    return [s["segment_id"] for s in BUILTIN_ASE_SEGMENTS]
