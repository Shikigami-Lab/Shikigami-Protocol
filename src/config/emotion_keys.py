"""Single source for emotion key lists used by wizard, autofill, API, and engines."""
from __future__ import annotations

from typing import FrozenSet, Tuple


BASE_EMOTION_KEYS: Tuple[str, ...] = (
    "calm",
    "joyful",
    "excited",
    "confident",
    "gentle",
    "grateful",
    "nostalgic",
    "thoughtful",
    "dreamy",
    "concerned",
    "playful_teasing",
    "sad",
    "disappointed",
    "angry",
    "sarcastic",
    "vulnerable",
    "tired",
)

BASE_EMOTION_KEYS_SET: FrozenSet[str] = frozenset(BASE_EMOTION_KEYS)


def emotion_keys_csv_for_wizard() -> str:
    """Comma-separated list for LLM system prompts (wizard / autofill)."""
    keys = BASE_EMOTION_KEYS
    return ", ".join(keys)


def emotion_keys_for_api() -> list[str]:
    """Ordered list for JSON API (settings UI)."""
    return list(BASE_EMOTION_KEYS)
