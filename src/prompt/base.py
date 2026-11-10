"""Base class for all prompt segments."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.session import Session
    from src.memory.conversation_store import ConversationStore

# Sentinel priority for history segments — they emit user/assistant messages,
# not system blocks.  Pipeline treats these specially.
HISTORY_PRIORITY = 90


def targets_chat(inject_into: str) -> bool:
    return inject_into == "chat"


def targets_reflection(inject_into: str) -> bool:
    return inject_into in ("reflection", "reflection_ase")


def targets_ase(inject_into: str) -> bool:
    return inject_into in ("ase", "reflection_ase")


def split_trigger_keywords(trigger_keywords: Optional[str]) -> List[str]:
    """comma/semicolon/newline-separated keywords; accepts full-width delimiters."""
    if not trigger_keywords:
        return []
    import re
    return [k.strip().lower() for k in re.split(r"[,，;；\n]", trigger_keywords) if k.strip()]


def evaluate_trigger_mode(
    trigger_mode: str,
    trigger_param: float,
    *,
    now: Optional[float] = None,
    last_fired: Optional[float] = None,
    fired_today: Optional[bool] = None,
    current_hour: Optional[int] = None,
    silence_minutes: Optional[float] = None,
    user_turn_count: Optional[int] = None,
    keyword_texts: Optional[List[str]] = None,
    trigger_keywords: Optional[str] = None,
) -> bool:
    """Single implementation of trigger-mode semantics.

    State is supplied by the caller — PromptSegment instances pass their own
    ``_last_fired``, custom segments pass nothing (stateless), the ASE engine
    passes its hint_last_used entries. Modes whose inputs are missing return
    False (except cooldown/once_per_day, where "never fired" means fire).
    Unknown modes default to True (fire), matching all legacy call sites.
    """
    import random

    now = time.time() if now is None else now

    if trigger_mode == "always":
        return True

    if trigger_mode == "probability":
        return random.random() < float(trigger_param)

    if trigger_mode == "cooldown":
        # trigger_param = cooldown in minutes
        if not last_fired:
            return True
        return (now - last_fired) / 60.0 >= float(trigger_param)

    if trigger_mode == "first_after_silence":
        # trigger_param = silence minutes before the triggering message
        if silence_minutes is None:
            return False
        return silence_minutes >= float(trigger_param)

    if trigger_mode == "once_per_day":
        if fired_today is None:
            if not last_fired:
                return True
            fired_today = (datetime.fromtimestamp(last_fired).strftime("%Y-%m-%d")
                           == datetime.fromtimestamp(now).strftime("%Y-%m-%d"))
        return not fired_today

    if trigger_mode == "every_n_turns":
        # trigger_param = N; fire on 1st, N+1th, 2N+1th user message
        if user_turn_count is None:
            return False
        return (user_turn_count + 1) % max(1, int(trigger_param)) == 0

    if trigger_mode == "first_turn_only":
        if user_turn_count is None:
            return False
        return user_turn_count == 0

    if trigger_mode == "time_window":
        # trigger_param = start_hour * 100 + end_hour (e.g. 510 = 5–10 点，闭区间)
        hour = current_hour if current_hour is not None else datetime.fromtimestamp(now).hour
        start = int(trigger_param) // 100
        end = int(trigger_param) % 100
        if start <= end:
            return start <= hour <= end
        return hour >= start or hour <= end

    if trigger_mode == "keyword":
        keys = split_trigger_keywords(trigger_keywords)
        if not keys:
            return False
        scan_text = " ".join(t for t in (keyword_texts or []) if t).lower()
        return any(k in scan_text for k in keys)

    return True


@dataclass
class SegmentResult:
    """What a segment builds: a list of LLM message dicts."""
    messages: List[Dict[str, str]] = field(default_factory=list)
    # Whether the segment actually fired (for cooldown bookkeeping)
    fired: bool = True
    # Target message: "system" (default) or "user" — controls where content is placed
    target: str = "system"


class PromptSegment(ABC):
    """Abstract base for every prompt segment.

    Subclasses MUST define:
        segment_id  (class-level str)
        priority    (class-level int, lower = earlier in system block)
        label       (class-level str, human-readable name for the UI)

    And implement:
        build(ctx) -> SegmentResult
    """

    # ── Class-level defaults (subclasses override) ──────────────────────────
    segment_id: str = ""
    priority: int = 50
    label: str = ""
    description: str = ""

    # Whether this segment can be disabled by the user (core segments cannot)
    is_core: bool = False

    # Dependency keys ("engine:emotion", "tool:timer", "feature:agent_tools",
    # "feature:memory", "capability:workspace") — evaluated by
    # src/prompt/dependencies.py.  An unmet dependency skips the segment in
    # every pipeline and locks its checkbox in the settings UI with a reason.
    # Never set on core segments: core segments must always run.
    requires: tuple = ()

    # 当 profiles/<id>/segments_config.json 里没有该 segment 的 overrides 时，是否默认启用（新 segment 可设为 False 以免打扰老用户）
    default_enabled: bool = True

    inject_into: str = "chat"   # "chat"|"reflection"|"ase"|"reflection_ase"
    is_readonly: bool = False    # True = content generated by code, not user-editable

    # Default trigger settings (can be overridden per-profile via SegmentMeta)
    # always | probability | cooldown | once_per_day | first_after_silence | every_n_turns | first_turn_only | time_window
    default_trigger_mode: str = "always"
    default_trigger_param: float = 1.0     # prob; or minutes; or N; or start_hour*100+end_hour

    # ── Instance state ───────────────────────────────────────────────────────
    def __init__(self):
        self._last_fired: float = 0.0  # epoch seconds

    # ── Trigger evaluation ───────────────────────────────────────────────────
    def is_triggered(
        self,
        trigger_mode: str,
        trigger_param: float,
        enabled: bool,
        ctx: Optional["BuildContext"] = None,
        trigger_keywords: Optional[str] = None,
    ) -> bool:
        """Return True if this segment should fire on this request."""
        if not enabled and not self.is_core:
            return False

        ctx_modes = ("first_after_silence", "once_per_day", "every_n_turns",
                     "first_turn_only", "time_window", "keyword")
        if trigger_mode in ctx_modes and ctx is None:
            return False

        kwargs: Dict[str, Any] = {"last_fired": self._last_fired or None}
        if ctx is not None:
            if trigger_mode == "first_after_silence":
                # build 在更新 last_user_message_time 之前调用，故 elapsed =
                # 自上一句用户消息以来的分钟数（本句前的沉默时长）。
                kwargs["silence_minutes"] = (time.time() - ctx.session.last_user_message_time) / 60.0
            elif trigger_mode == "once_per_day":
                today = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d")
                kwargs["fired_today"] = ctx.session.get_segment_last_fired_date(self.segment_id) == today
            elif trigger_mode in ("every_n_turns", "first_turn_only"):
                user_name = ctx.extras.get("user_name") or "用户"
                kwargs["user_turn_count"] = ctx.store.total_user_messages_count(user_name)
            elif trigger_mode == "time_window":
                kwargs["current_hour"] = ctx.extras.get("current_hour")
            elif trigger_mode == "keyword":
                # trigger_param = scan_turns (default 10)
                scan_turns = max(1, min(50, int(trigger_param) if trigger_param else 10))
                parts = [ctx.user_msg or ""]
                try:
                    recent = ctx.store.get_recent(scan_turns)
                    parts.extend(turn.get("content", "") for turn in recent)
                except Exception:
                    pass
                kwargs["keyword_texts"] = parts
                kwargs["trigger_keywords"] = trigger_keywords

        return evaluate_trigger_mode(trigger_mode, trigger_param, **kwargs)

    def mark_fired(self):
        self._last_fired = time.time()

    # ── Build ────────────────────────────────────────────────────────────────
    @abstractmethod
    def build(self, ctx: "BuildContext") -> SegmentResult:
        """Produce the messages for this segment.

        Must return a SegmentResult (even if empty).
        """
        ...


@dataclass
class BuildContext:
    """All data available to a segment when building."""
    session: "Session"
    profile: Dict[str, Any]
    store: "ConversationStore"
    user_msg: str
    n_history_turns: int = 20
    preset: Optional[Dict[str, Any]] = None
    # Extra runtime data injected by the pipeline (engines, etc.)
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReflectionBuildContext:
    """Context passed to reflection and ASE segment build() methods."""
    profile_id: str
    profile: Dict[str, Any]
    recent_turns: List[Dict]
    emotion: Optional[Dict]
    affinity: Optional[Dict]
    silent_seconds: float
    persona_name: str
    persona_brief: str
    ase_context: Optional[Dict]
    prev_reflection: Optional[Dict]
    context_notes: List[str]
    ase_decision_context: Optional[Dict]
    last_user_message_time: Optional[float]
    user_name: str = "用户"
    user_persona: Dict[str, Any] = field(default_factory=dict)  # merged global+profile
    locale: str = "zh"
    hint_last_used: Dict[str, Any] = field(default_factory=dict)
    store: Optional[Any] = None           # ConversationStore — for user_engagement stats
    memory_manager: Optional[Any] = None  # MemoryManager — for memory_facts segment
    reflection_cfg: Dict[str, Any] = field(default_factory=dict)  # global reflection config
    # 主动话题发现：topic_candidates 段落填充候选，_reflect() 据此把 topic_pick 解析成 chosen_topic
    topic_candidates: List[Any] = field(default_factory=list)        # List[TopicCandidate]
    topic_discovery_cfg: Dict[str, Any] = field(default_factory=dict)
    session: Optional[Any] = None
    app: Optional[Any] = None
