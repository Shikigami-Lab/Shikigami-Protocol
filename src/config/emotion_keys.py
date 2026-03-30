"""Single source for emotion key lists used by wizard, autofill, API, and engines.

Extended keys live in emotion_keys_extended.py (private/full build only; omitted from public sync).
"""
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


def _extended_emotion_keys() -> Tuple[str, ...]:
    try:
        from src.config.emotion_keys_extended import EXTENDED_EMOTION_KEYS

        return tuple(EXTENDED_EMOTION_KEYS)
    except ImportError:
        return ()


def all_emotion_keys_tuple() -> Tuple[str, ...]:
    """Full key order: base prefix through playful_teasing, then extended keys (if any), then rest."""
    extended = _extended_emotion_keys()
    if not extended:
        return BASE_EMOTION_KEYS
    # base = first 11 keys + last 6 keys (playful_teasing at index 10)
    return BASE_EMOTION_KEYS[:11] + extended + BASE_EMOTION_KEYS[11:]


def all_emotion_keys_set() -> FrozenSet[str]:
    return frozenset(all_emotion_keys_tuple())


def emotion_keys_csv_for_wizard(*, public_mode: bool) -> str:
    """Comma-separated list for LLM system prompts (wizard / autofill)."""
    keys = BASE_EMOTION_KEYS  # [public] extended keys omitted
    return ", ".join(keys)


def emotion_keys_for_api(*, public_mode: bool) -> list[str]:
    """Ordered list for JSON API (settings UI)."""
    return list(BASE_EMOTION_KEYS)  # [public] extended keys omitted
