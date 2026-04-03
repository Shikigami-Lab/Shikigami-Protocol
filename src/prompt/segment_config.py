"""Per-profile segment configuration — stored in profiles/<id>/segments_config.json."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from src.utils.paths import get_project_root

logger = logging.getLogger(__name__)

_PROFILES_DIR = os.path.join(get_project_root(), "profiles")


@dataclass
class SegmentMeta:
    """Per-profile override for a single registered segment."""
    segment_id: str
    enabled: bool = True
    priority: Optional[int] = None          # None = use segment class default
    trigger_mode: Optional[str] = None      # None = use segment class default
    trigger_param: Optional[float] = None   # None = use segment class default
    content: Optional[str] = None           # for ASE segments with user-editable content
    trigger_keywords: Optional[str] = None  # comma-separated keywords for "keyword" trigger mode

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SegmentMeta":
        return cls(
            segment_id=d["segment_id"],
            enabled=d.get("enabled", True),
            priority=d.get("priority"),
            trigger_mode=d.get("trigger_mode"),
            trigger_param=d.get("trigger_param"),
            content=d.get("content"),
            trigger_keywords=d.get("trigger_keywords"),
        )


@dataclass
class CustomSegmentDef:
    """A user-defined custom segment (injected system text)."""
    segment_id: str               # unique, user-chosen (e.g. "my_custom_1")
    label: str                    # display name
    content: str                  # raw system text to inject
    priority: int = 50
    enabled: bool = True
    trigger_mode: str = "always"  # always | probability | cooldown | once_per_day | first_after_silence | every_n_turns | first_turn_only | time_window | keyword
    trigger_param: float = 1.0
    inject_into: str = "chat"
    trigger_keywords: Optional[str] = None  # comma-separated keywords for "keyword" trigger mode

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CustomSegmentDef":
        return cls(
            segment_id=d["segment_id"],
            label=d.get("label", d["segment_id"]),
            content=d.get("content", ""),
            priority=d.get("priority", 50),
            enabled=d.get("enabled", True),
            trigger_mode=d.get("trigger_mode", "always"),
            trigger_param=d.get("trigger_param", 1.0),
            inject_into=d.get("inject_into", "chat"),
            trigger_keywords=d.get("trigger_keywords"),
        )


@dataclass
class SegmentConfig:
    """All segment configuration for one profile."""
    overrides: Dict[str, SegmentMeta] = field(default_factory=dict)
    custom_segments: List[CustomSegmentDef] = field(default_factory=list)

    # ── Accessors ────────────────────────────────────────────────────────────
    def get_meta(self, segment_id: str) -> Optional[SegmentMeta]:
        return self.overrides.get(segment_id)

    def set_meta(self, meta: SegmentMeta):
        self.overrides[meta.segment_id] = meta

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overrides": {k: v.to_dict() for k, v in self.overrides.items()},
            "custom_segments": [c.to_dict() for c in self.custom_segments],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SegmentConfig":
        overrides = {
            k: SegmentMeta.from_dict(v)
            for k, v in d.get("overrides", {}).items()
        }
        customs = [
            CustomSegmentDef.from_dict(c)
            for c in d.get("custom_segments", [])
        ]
        return cls(overrides=overrides, custom_segments=customs)


# ── I/O helpers ──────────────────────────────────────────────────────────────

def _config_path(profile_id: str) -> str:
    return os.path.join(_PROFILES_DIR, profile_id, "segments_config.json")


def effective_segment_enabled(meta: Optional[SegmentMeta], seg_cls) -> bool:
    """无 overrides 时使用 seg_cls.default_enabled（默认 True）；有 meta 时用 meta.enabled。"""
    if meta is not None:
        return meta.enabled
    return getattr(seg_cls, "default_enabled", True)


def load_segment_config(profile_id: str) -> SegmentConfig:
    """Load segment config for a profile, returning empty config if not found."""
    path = _config_path(profile_id)
    if not os.path.exists(path):
        return SegmentConfig()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return SegmentConfig.from_dict(data)
    except Exception as e:
        logger.warning(f"[SegmentConfig] load failed for '{profile_id}': {e}")
        return SegmentConfig()


def save_segment_config(profile_id: str, config: SegmentConfig):
    """Persist segment config for a profile."""
    path = _config_path(profile_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[SegmentConfig] save failed for '{profile_id}': {e}")
