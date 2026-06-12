"""Prompt pipeline — assembles the full message list using the segment registry.

Message ordering (all by effective priority, 0 = 最先出现, 90 = 最靠近 user message):
  [merged system message] — 所有 system 片段按 priority 升序排列（含注册 segment 与自定义 segment）
  [user/assistant history] — HistorySegment (priority == 90) 输出的对话轮
  [current user message]   — 始终最后

Segment cooldown (e.g. TodosSegment 300min): _last_fired is synced from session's
persisted segment_last_fired before trigger check, and written back after mark_fired,
so cooldown survives server restarts.

Debug: 每次 build_messages 会记录各 segment 与总耗时到 _last_build_timings，
chat 流结束后可配合 LLM 流式用时一并写入 build_timings.log。
"""
import logging
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

# 最近一次 build 的耗时（供 debug：build_timings.log）
_last_build_timings: Dict[str, Any] = {}

from src.config.profile_loader import ProfileLoader
from src.core.session import Session
from src.memory.conversation_store import ConversationStore
from src.prompt.base import HISTORY_PRIORITY, BuildContext, PromptSegment, SegmentResult, targets_chat, targets_ase
from src.prompt.registry import get_registered
from src.prompt.segment_config import CustomSegmentDef, SegmentMeta, effective_segment_enabled, load_segment_config

# ── Import all built-in segment modules to trigger @register decorators ───────
import src.prompt.segments.persona          # noqa: F401
import src.prompt.segments.user_persona     # noqa: F401
import src.prompt.segments.history          # noqa: F401
import src.prompt.segments.time_context     # noqa: F401
import src.prompt.segments.emotion_state    # noqa: F401
import src.prompt.segments.energy_state     # noqa: F401
import src.prompt.segments.affinity_state   # noqa: F401
import src.prompt.segments.long_term_facts  # noqa: F401
import src.prompt.segments.mid_term_memory  # noqa: F401
import src.tools.todo.segment                # noqa: F401  (TodosSegment, cooldown 5h)
import src.tools.timer.segment               # noqa: F401  (ActiveTimersSegment)
import src.prompt.segments.reflection_state  # noqa: F401  (ReflectionStateSegment)
import src.prompt.segments.current_context  # noqa: F401  (单聊时注明当前为一对一私聊)
import src.prompt.segments.lorebook         # noqa: F401  (LoreBookSegment, keyword-triggered)
import src.prompt.segments.trend_awareness  # noqa: F401  (TrendAwarenessSegment, cooldown 4h；段落设置可关)
import src.prompt.segments.weather          # noqa: F401  (WeatherSegment, cooldown 2h)
import src.prompt.segments.reflection.persona        # noqa: F401
import src.prompt.segments.reflection.ase_config     # noqa: F401
import src.prompt.segments.reflection.prev_thought   # noqa: F401
import src.prompt.segments.reflection.emotion        # noqa: F401
import src.prompt.segments.reflection.affinity       # noqa: F401
import src.prompt.segments.reflection.silence        # noqa: F401
import src.prompt.segments.reflection.ase_context    # noqa: F401
import src.prompt.segments.reflection.recent_dialogue # noqa: F401
import src.prompt.segments.ase.morning_greeting      # noqa: F401
import src.prompt.segments.ase.long_silence_check_in # noqa: F401
import src.prompt.segments.ase.initiation_frame      # noqa: F401
import src.prompt.segments.ase.behavioral_guidance   # noqa: F401
import src.prompt.segments.ase.scene_context         # noqa: F401
import src.prompt.segments.health_mode               # noqa: F401

logger = logging.getLogger(__name__)

_profile_loader = ProfileLoader()

# Segment instances cached per profile so that cooldown state (_last_fired)
# is isolated between profiles.  Structure: profile_id → segment_id → instance.
_segment_instances: Dict[str, Dict[str, PromptSegment]] = {}


def _get_instance(seg_cls, profile_id: str) -> PromptSegment:
    profile_cache = _segment_instances.setdefault(profile_id, {})
    sid = seg_cls.segment_id
    if sid not in profile_cache:
        profile_cache[sid] = seg_cls()
    return profile_cache[sid]


def get_last_build_timings() -> Dict[str, Any]:
    """返回最近一次 build_messages 的耗时统计（含各 segment 与总 build 时间）。"""
    return dict(_last_build_timings)


def build_messages(
    session: Session,
    user_msg: str,
    store: ConversationStore,
    n_history_turns: int = 20,
    preset: dict = None,
    app=None,
    extra_system: str = "",
    max_facts_override: Optional[int] = None,
    group_reply_as_sender: Optional[str] = None,
    group_user_name: Optional[str] = None,
    last_message_override: Optional[Dict[str, str]] = None,
    out_extras: Optional[Dict[str, Any]] = None,
    current_group_id: Optional[str] = None,
    current_group_gname: Optional[str] = None,
    pipeline_mode: str = "chat",
    extra_context: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """Assemble the full message list for the LLM.

    pipeline_mode: "chat" (default), "ase", or "reflection" — controls which
                   segments are included based on their inject_into value.
    extra_context: additional key-value pairs merged into BuildContext.extras
                   (used by ASE to pass vlm_description, last_ase_content, etc.
                   to ASE-specific segments).
    extra_system: if non-empty, appended to the merged system message (used by
                  ASE heartbeat_speak to inject heartbeat context without adding
                  a second system message).
    max_facts_override: if set, overrides max_facts_in_prompt for this request
                        (used by game mode to reduce context length).
    group_reply_as_sender: when set (group chat), history segment treats other AIs'
                          assistant messages as user with "[sender]: content".
    group_user_name: user display name in group (for group chat context).
    last_message_override: when set (e.g. group chat 2nd+ reply), this message is
                            appended as the last user message instead of user_msg,
                            so the model replies to the previous speaker.
    current_group_id / current_group_gname: when in group chat, history segment
                            may use merged timeline (single + all groups) and
                            transforms only the current group's other AIs to [sender]: content.
    """
    global _last_build_timings
    build_start = time.perf_counter()
    _last_build_timings = {"segments": {}, "profile_load_ms": 0.0, "assemble_ms": 0.0, "total_build_ms": 0.0}

    # Load profile card
    t0 = time.perf_counter()
    try:
        profile = _profile_loader.load(session.profile_id)
    except FileNotFoundError:
        logger.warning(f"[Pipeline] profile '{session.profile_id}' not found, using empty persona")
        profile = {}

    # Load per-profile segment configuration (enabled/priority/trigger overrides)
    seg_cfg = load_segment_config(session.profile_id)
    _last_build_timings["profile_load_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    extras: Dict[str, Any] = {}
    if app:
        extras["app"] = app
        _cfg = getattr(getattr(app, "state", None), "config", None)
        extras["user_name"] = getattr(_cfg, "user_name", "用户") or "用户"
        extras["user_persona_global"] = getattr(_cfg, "user_persona", {}) or {}
    if max_facts_override is not None:
        extras["max_facts_override"] = max_facts_override
    if group_reply_as_sender is not None:
        extras["group_reply_as_sender"] = group_reply_as_sender
    if group_user_name is not None:
        extras["group_user_name"] = group_user_name
    if current_group_id is not None:
        extras["current_group_id"] = current_group_id
    if current_group_gname is not None:
        extras["current_group_gname"] = current_group_gname
    extras["current_hour"] = datetime.fromtimestamp(time.time()).hour
    if extra_context:
        extras.update(extra_context)

    ctx = BuildContext(
        session=session,
        profile=profile,
        store=store,
        user_msg=user_msg,
        n_history_turns=n_history_turns,
        preset=preset or {},
        extras=extras,
    )

    # (priority, msg) for every system message — 0 = 最先出现，90 = 最靠近 user message
    sys_with_priority: List[tuple] = []  # List[Tuple[int, Dict[str, str]]]
    prefix_non_system: List[Dict[str, str]] = []
    suffix_non_system: List[Dict[str, str]] = []
    history_msgs: List[Dict[str, str]] = []
    # Segments with target="user": (priority, content_str) — appended to final user message
    user_seg_parts: List[tuple] = []

    # ── Registered segments ───────────────────────────────────────────────────
    def _seg_matches_mode(inject_into: str) -> bool:
        if pipeline_mode == "ase":
            return targets_ase(inject_into) or targets_chat(inject_into)
        return targets_chat(inject_into)

    for seg_cls in [cls for cls in get_registered() if _seg_matches_mode(cls.inject_into)]:
        seg = _get_instance(seg_cls, session.profile_id)
        meta: Optional[SegmentMeta] = seg_cfg.get_meta(seg_cls.segment_id)

        # Resolve effective settings — meta overrides class defaults
        enabled       = effective_segment_enabled(meta, seg_cls)
        # [public] health_mode force-enable omitted
        trigger_mode  = (meta.trigger_mode  if meta and meta.trigger_mode  is not None
                         else seg_cls.default_trigger_mode)
        trigger_param = (meta.trigger_param if meta and meta.trigger_param is not None
                         else seg_cls.default_trigger_param)
        eff_priority  = (meta.priority      if meta and meta.priority      is not None
                         else seg_cls.priority)

        # Cooldown: use persisted last_fired so restart does not reset (e.g. todos 300min)
        if trigger_mode == "cooldown":
            persisted = session.get_segment_last_fired(seg.segment_id)
            seg._last_fired = max(seg._last_fired, persisted)

        trigger_keywords = (meta.trigger_keywords if meta and meta.trigger_keywords is not None
                            else getattr(seg_cls, "default_trigger_keywords", None))
        if not seg.is_triggered(trigger_mode, trigger_param, enabled, ctx=ctx,
                                trigger_keywords=trigger_keywords):
            logger.debug(f"[Pipeline] '{seg_cls.segment_id}' skipped (trigger={trigger_mode})")
            continue

        try:
            seg_start = time.perf_counter()
            result: SegmentResult = seg.build(ctx)
            _last_build_timings["segments"][seg.segment_id] = round((time.perf_counter() - seg_start) * 1000, 2)
        except Exception as e:
            logger.error(f"[Pipeline] segment '{seg_cls.segment_id}' error: {e}", exc_info=True)
            continue

        if not result.messages:
            continue

        seg.mark_fired()
        session.set_segment_last_fired(seg.segment_id, time.time())
        if trigger_mode == "once_per_day":
            session.set_segment_last_fired_date(seg.segment_id, datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d"))

        for m in result.messages:
            if getattr(result, "target", "system") == "user":
                # Segment requests its content go into the final user message
                user_seg_parts.append((eff_priority, m.get("content", "")))
            elif m.get("role") == "system":
                sys_with_priority.append((eff_priority, m))
            elif eff_priority < HISTORY_PRIORITY:
                prefix_non_system.append(m)
            elif eff_priority == HISTORY_PRIORITY:
                history_msgs.append(m)
            else:
                suffix_non_system.append(m)

    # ── Custom (user-defined) segments ────────────────────────────────────────
    today_str = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d")
    custom_segs = [c for c in seg_cfg.custom_segments if _seg_matches_mode(c.inject_into)]
    for custom in custom_segs:
        if not _custom_triggered(custom, ctx):
            continue
        content = (custom.content or "").strip()
        if not content:
            continue
        msg = {"role": "system", "content": content}
        sys_with_priority.append((custom.priority, msg))
        if getattr(custom, "trigger_mode", None) == "once_per_day":
            session.set_segment_last_fired_date(custom.segment_id, today_str)

    # ── Assemble ──────────────────────────────────────────────────────────────
    # Merge ALL system blocks into one, ordered by priority ascending (0 最先，89 最靠近 history)。
    assemble_start = time.perf_counter()
    messages: List[Dict[str, str]] = []

    sys_with_priority.sort(key=lambda x: x[0])
    sys_parts: List[str] = [m["content"] for _, m in sys_with_priority]

    if extra_system:
        sys_parts.append(extra_system.strip())

    if sys_parts:
        messages.append({"role": "system", "content": "\n\n".join(sys_parts)})

    messages.extend(prefix_non_system)
    messages.extend(history_msgs)
    messages.extend(suffix_non_system)

    # Merge user-targeted segment content into the final user message
    if user_seg_parts:
        user_seg_parts.sort(key=lambda x: x[0])
        user_seg_text = "\n\n".join(part for _, part in user_seg_parts if part.strip())
    else:
        user_seg_text = ""

    if last_message_override is not None and last_message_override.get("content") is not None:
        if user_seg_text:
            last_message_override = dict(last_message_override)
            orig = last_message_override.get("content", "")
            last_message_override["content"] = user_seg_text + ("\n\n" + orig if orig.strip() else "")
        messages.append(last_message_override)
    else:
        final_user = user_seg_text + ("\n\n" + user_msg if user_msg and user_msg.strip() else "")
        final_user = final_user.strip()
        # 群聊等场景下 store 已先 append 了当前 user 消息，history 末尾已是 user_msg，避免重复
        last = messages[-1] if messages else None
        if not (last and last.get("role") == "user" and (last.get("content") or "").strip() == (final_user or "").strip()):
            messages.append({"role": "user", "content": final_user})

    _last_build_timings["assemble_ms"] = round((time.perf_counter() - assemble_start) * 1000, 2)
    _last_build_timings["total_build_ms"] = round((time.perf_counter() - build_start) * 1000, 2)
    if out_extras is not None:
        out_extras.update(ctx.extras)
    return messages


def _custom_triggered(custom: CustomSegmentDef, ctx: BuildContext) -> bool:
    """Evaluate trigger condition for a custom segment."""
    if not custom.enabled:
        return False
    mode = getattr(custom, "trigger_mode", "always")
    param = getattr(custom, "trigger_param", 1.0)

    if mode == "always":
        return True
    if mode == "probability":
        return random.random() < param
    if mode == "cooldown":
        # Custom segments: no persisted cooldown, treat as always
        return True
    if mode == "first_after_silence":
        elapsed = (time.time() - ctx.session.last_user_message_time) / 60.0
        return elapsed >= param
    if mode == "once_per_day":
        today = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d")
        last = ctx.session.get_segment_last_fired_date(custom.segment_id)
        return last != today
    if mode == "every_n_turns":
        user_name = ctx.extras.get("user_name") or "用户"
        n = ctx.store.total_user_messages_count(user_name)
        return (n + 1) % max(1, int(param)) == 0
    if mode == "first_turn_only":
        user_name = ctx.extras.get("user_name") or "用户"
        return ctx.store.total_user_messages_count(user_name) == 0
    if mode == "time_window":
        current_hour = ctx.extras.get("current_hour")
        if current_hour is None:
            current_hour = datetime.fromtimestamp(time.time()).hour
        start = int(param) // 100
        end = int(param) % 100
        if start <= end:
            return start <= current_hour <= end
        return current_hour >= start or current_hour <= end
    if mode == "keyword":
        kw = getattr(custom, "trigger_keywords", None) or ""
        keys = [k.strip().lower() for k in __import__('re').split(r"[,，;；\n]", kw) if k.strip()]
        if not keys:
            return False
        scan_turns = max(1, min(50, int(param) if param else 10))
        parts = [ctx.user_msg or ""]
        try:
            recent = ctx.store.get_recent(scan_turns)
            parts.extend(turn.get("content", "") for turn in recent)
        except Exception:
            pass
        scan_text = " ".join(parts).lower()
        return any(k in scan_text for k in keys)
    return True
