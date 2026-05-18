"""Status API — GET/POST /sessions/{session_id}/status
                GET /sessions/{session_id}/ase_status

GET  → 返回当前 session 的 emotion_state + affinity_state + available_emotions
POST → 手动微调 energy_level / primary_emotion / affinity（Debug / 高级用户）
GET ase_status → 返回 ASE 各门限实时状态 + reflection 自省内容（供工具栏面板）
"""
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.engines.emotion_engine import EmotionEngine
from src.engines.affinity_engine import AffinityEngine
from src.config.profile_loader import ProfileLoader

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/engine_warnings")
async def get_engine_warnings(request: Request):
    """返回当前辅助引擎健康警告列表（前端状态栏轮询用）。"""
    warnings: dict = getattr(request.app.state, "engine_warnings", {})
    return {"warnings": list(warnings.values()), "ok": len(warnings) == 0}


class StatusAdjust(BaseModel):
    energy_level: Optional[float] = None
    primary_emotion: Optional[str] = None
    affinity: Optional[float] = None


@router.get("/sessions/{session_id}/status")
async def get_status(session_id: str, request: Request):
    sm = request.app.state.session_manager
    # Use already-loaded session if available; otherwise create a temporary one for
    # reading state files without auto-registering the profile as an active session.
    session = sm._sessions.get(session_id)
    if session is None:
        from src.core.session import Session
        try:
            card = ProfileLoader().load(session_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Session not found")
        session = Session.from_profile(session_id, card.get("display_name", session_id))
        session.load_runtime_state()
        # Intentionally NOT adding to sm._sessions — profile is unloaded from chat

    emotion_engine: EmotionEngine = getattr(request.app.state, "emotion_engine", None)
    affinity_engine: AffinityEngine = getattr(request.app.state, "affinity_engine", None)

    emotion_state = emotion_engine.load_state(session) if emotion_engine else {}
    affinity_state = affinity_engine.load_state(session) if affinity_engine else {}

    # 从 profile 获取可用情绪列表（供前端情绪选择器用）
    profile = ProfileLoader().load(session.profile_id) or {}

    available_emotions: list = []
    if emotion_engine:
        available_emotions = list(emotion_engine.get_emotion_prompts(profile).keys())

    return {
        "emotion_state": emotion_state,
        "affinity_state": affinity_state,
        "available_emotions": available_emotions,
    }


@router.get("/sessions/{session_id}/user_activity")
async def get_user_activity(session_id: str, request: Request):
    """返回该人格的合并用户活动统计（单聊+其参与的所有群），供频率面板使用。session_id 即 profile_id。"""
    sm = getattr(request.app.state, "session_manager", None)
    if sm is None:
        raise HTTPException(status_code=503, detail="Session manager not available")
    session = sm.get_by_id(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    profile_id = getattr(session, "profile_id", None) or session_id
    config = getattr(request.app.state, "config", None)
    user_name = (getattr(config, "user_name", None) or "用户") or "用户"

    from src.memory.merged_history import get_merged_user_activity
    activity = get_merged_user_activity(profile_id, request.app, user_name)

    cadence_hint = ""
    tracker = getattr(request.app.state, "cadence_tracker", None)
    if tracker:
        signals = tracker.get_signals(profile_id)
        if getattr(signals, "has_data", False):
            if getattr(signals, "is_quick", False):
                cadence_hint = "回复较频繁"
            elif getattr(signals, "is_slow", False):
                cadence_hint = "回复较慢"

    return {
        "last_user_ts": activity.get("last_user_ts"),
        "total_count": activity.get("total_count", 0),
        "count_today": activity.get("count_today", 0),
        "count_24h": activity.get("count_24h", 0),
        "count_7d": activity.get("count_7d", 0),
        "cadence_hint": cadence_hint,
    }


@router.post("/sessions/{session_id}/status")
async def adjust_status(session_id: str, body: StatusAdjust, request: Request):
    sm = request.app.state.session_manager
    session = sm.get_by_id(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    emotion_engine: EmotionEngine = getattr(request.app.state, "emotion_engine", None)
    affinity_engine: AffinityEngine = getattr(request.app.state, "affinity_engine", None)

    changes = {}

    if emotion_engine and (body.energy_level is not None or body.primary_emotion is not None):
        state = emotion_engine.load_state(session)
        if body.energy_level is not None:
            state["energy_level"] = max(0.0, min(100.0, float(body.energy_level)))
            changes["energy_level"] = state["energy_level"]
        if body.primary_emotion is not None:
            profile = ProfileLoader().load(session.profile_id) or {}
            valid = list(emotion_engine.get_emotion_prompts(profile).keys())
            if body.primary_emotion not in valid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown emotion '{body.primary_emotion}'. Valid: {valid}",
                )
            state["primary_emotion"] = body.primary_emotion
            state["primary_weight"]  = 1.0
            state["secondary_emotion"] = None
            state["secondary_weight"]  = 0.0
            state["tertiary_emotion"]  = None
            state["tertiary_weight"]   = 0.0
            state["emotion_layers"] = [{"emotion": body.primary_emotion, "intensity": 1.0}]
            changes["primary_emotion"] = body.primary_emotion
        import time
        state["last_updated"] = time.time()
        emotion_engine.save_state(session, state)

    if affinity_engine and body.affinity is not None:
        from src.engines.affinity_engine import _resolve_status
        state = affinity_engine.load_state(session)
        state["affinity"] = round(float(body.affinity), 2)
        state["status"] = _resolve_status(state["affinity"])
        affinity_engine.save_state(session, state)
        changes["affinity"] = state["affinity"]
        changes["status"] = state["status"]

    logger.info("[status] manual adjust session=%s changes=%s", session_id, changes)

    return {"ok": True, "changes": changes}


@router.get("/sessions/{session_id}/ase_status")
async def get_ase_status(session_id: str, request: Request):
    """Return real-time ASE gate status + reflection state for the toolbar panel."""
    sm = request.app.state.session_manager
    session = sm.get_by_id(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    config = request.app.state.config
    ase_cfg = config.get_ase_config()
    ase_enabled = bool(ase_cfg.get("enabled", False))
    # 人格级覆盖：该人格单独关闭 ASE 时，UI 应显示「已禁用」
    try:
        from src.config.effective_config import get_effective_engine_config
        _eff_ase = get_effective_engine_config(request.app, session.profile_id, "ase")
        if _eff_ase.get("enabled", True) is False:
            ase_enabled = False
    except Exception:
        pass
    mode_name = getattr(session, "ase_mode", "medium")
    # 与 ASE 引擎一致：session 未显式设置时使用配置中的默认模式，UI 才能显示真实模式（高/低/游戏等）
    if mode_name == "medium":
        mode_name = ase_cfg.get("mode", "medium")
    modes = ase_cfg.get("modes", {})
    mode_cfg = modes.get(mode_name, modes.get("medium", {}))

    # ── Runtime state from AseEngine._state ──────────────────────────────────
    ase_engine = getattr(request.app.state, "ase_engine", None)
    profile_state: dict = {}
    if ase_engine and hasattr(ase_engine, "_state"):
        profile_state = ase_engine._state.get(session.profile_id, {})

    now = time.time()
    global_last = getattr(request.app.state, "last_user_message_time", 0.0)
    effective_last = max(session.last_user_message_time, global_last)
    silent_seconds = now - effective_last

    min_silent = float(mode_cfg.get("min_silent_seconds", 300))
    urgency_threshold = float(mode_cfg.get("urgency_threshold", 0.35))
    min_interval = float(mode_cfg.get("min_interval", 600))
    max_per_24h = int(mode_cfg.get("max_per_24h", 30))
    max_consecutive = int(mode_cfg.get("max_consecutive_without_response") or 0)
    check_interval = float(mode_cfg.get("check_interval", 300))

    reflection_state = getattr(session, "reflection_state", None) or {}
    urgency = float(reflection_state.get("urgency", 0.0))

    # 24h speak count
    cutoff = now - 86400
    timestamps = [t for t in profile_state.get("timestamps_24h", []) if t >= cutoff]
    today_count = len(timestamps)

    # Cooldown
    last_speak_time = profile_state.get("last_speak_time")
    last_speak_ago = (now - last_speak_time) if last_speak_time else None
    cooldown_required = min_interval * (1.0 - urgency * 0.85) if last_speak_time else 0.0
    cooldown_remaining = max(0.0, cooldown_required - (now - last_speak_time)) if last_speak_time else 0.0

    consecutive = int(profile_state.get("consecutive_speaks", 0))
    # If user has replied since the last speak, effective consecutive is 0
    # (may not yet be flushed to disk by _check_speak's reset logic)
    if last_speak_time and session.last_user_message_time > last_speak_time:
        consecutive = 0

    # Next ASE check countdown（自适应睡眠时用 next_check_at，否则用固定间隔）
    last_check_time = profile_state.get("last_check_time")
    next_check_at = profile_state.get("next_check_at")
    if next_check_at is not None and next_check_at > 0:
        next_check_in = round(max(0.0, next_check_at - now))
    elif last_check_time:
        next_check_in = round(max(0.0, check_interval - (now - last_check_time)))
    else:
        next_check_in = None
    this_round_sleep_secs = profile_state.get("this_round_sleep_secs")
    if this_round_sleep_secs is None:
        this_round_sleep_secs = check_interval
    # Next earliest possible speak = max(next check, cooldown remaining)
    next_speak_earliest = max(next_check_in or 0, cooldown_remaining)

    # ── Determine overall status ──────────────────────────────────────────────
    if not ase_enabled:
        overall = "disabled"
    elif today_count >= max_per_24h:
        overall = "daily_limit"
    elif max_consecutive > 0 and consecutive >= max_consecutive:
        overall = "consecutive_limit"
    elif silent_seconds < min_silent:
        overall = "waiting_silence"
    elif urgency < urgency_threshold:
        overall = "waiting_urgency"
    elif cooldown_remaining > 0:
        overall = "waiting_cooldown"
    elif next_check_in and next_check_in > 0:
        overall = "waiting_check"   # all gates pass, but check cycle hasn't fired yet
    else:
        overall = "ready"

    # Combined floor: min seconds before all TIME-based gates clear (urgency excluded — it's dynamic)
    silence_remaining = round(max(0.0, min_silent - silent_seconds))
    if overall in ("disabled", "daily_limit", "consecutive_limit"):
        next_speak_floor = None   # hard-blocked, no ETA
    else:
        next_speak_floor = round(max(next_check_in or 0, float(cooldown_remaining), float(silence_remaining)))

    # ── Reflection state ──────────────────────────────────────────────────────
    refl_cfg = config.get_reflection_config()
    refl_interval = int(refl_cfg.get("interval_seconds", 180))
    refl_idle_after = float(refl_cfg.get("idle_throttle_after_seconds", 0))
    refl_idle_interval = int(refl_cfg.get("idle_interval_seconds", refl_interval * 5))
    refl_enabled = bool(refl_cfg.get("enabled", False))
    # 人格级覆盖：该人格单独关闭自省时，UI 应显示「自省已禁用」并隐藏过期内容
    try:
        from src.config.effective_config import get_effective_engine_config
        _eff_refl = get_effective_engine_config(request.app, session.profile_id, "reflection")
        if _eff_refl.get("enabled", True) is False:
            refl_enabled = False
    except Exception:
        pass

    refl_is_idle = refl_idle_after > 0 and silent_seconds >= refl_idle_after
    effective_refl_interval = refl_idle_interval if refl_is_idle else refl_interval

    refl_updated_at = reflection_state.get("updated_at")
    refl_updated_ago = round(now - refl_updated_at) if refl_updated_at else None

    # Read actual next-run timestamp from the engine (set just before each sleep).
    # Falls back to estimation only if engine is not available.
    refl_engine = getattr(request.app.state, "reflection_engine", None)
    next_run_at = getattr(refl_engine, "_next_run_at", 0.0)
    last_skip_reason = getattr(refl_engine, "_last_skip_reason", "")
    refl_task_alive = (
        refl_engine is not None
        and refl_engine._task is not None
        and not refl_engine._task.done()
    )
    if next_run_at > now:
        refl_next_in = round(next_run_at - now)
    elif refl_updated_ago is not None:
        # Engine hasn't set _next_run_at yet (first start) — fall back to estimate
        refl_next_in = max(0, effective_refl_interval - refl_updated_ago)
    elif refl_task_alive:
        # Engine is running but first reflection hasn't completed yet —
        # show full interval as estimated countdown so the UI bar appears.
        refl_next_in = effective_refl_interval
    else:
        refl_next_in = None

    reflection_out = {
        # 自省被禁用时清空内容，避免 UI 继续显示上一次的过期自省
        "thought": reflection_state.get("thought", "") if refl_enabled else "",
        "urgency": urgency,
        "topic_anchor": reflection_state.get("topic_anchor", "") if refl_enabled else "",
        # 主动话题发现：当前选定话题的来源（供面板显示「· 来自对话回忆」）
        "chosen_topic_source": (
            (reflection_state.get("chosen_topic") or {}).get("source", "")
            if refl_enabled else ""
        ),
        "updated_ago": refl_updated_ago,
        "next_in": refl_next_in,           # 距下次自省的真实剩余秒数
        "interval": effective_refl_interval,
        "is_idle": refl_is_idle,
        "enabled": refl_enabled,
        "last_skip_reason": last_skip_reason,  # 最近一次跳过原因
    }

    return {
        "ase_enabled": ase_enabled,
        "ase_mode": mode_name,
        "overall_status": overall,
        # silence gate
        "silent_seconds": round(silent_seconds),
        "min_silent_seconds": round(min_silent),
        # urgency gate
        "urgency": round(urgency, 3),
        "urgency_threshold": round(urgency_threshold, 3),
        # cooldown gate
        "last_speak_ago": round(last_speak_ago) if last_speak_ago is not None else None,
        "last_speak_time": last_speak_time,
        "min_interval": round(min_interval),
        "cooldown_remaining": round(cooldown_remaining),
        # 24h gate
        "today_speak_count": today_count,
        "max_per_24h": max_per_24h,
        # consecutive gate
        "consecutive_speaks": consecutive,
        "max_consecutive": max_consecutive,
        # timing
        "check_interval": round(check_interval),
        "next_check_in": next_check_in,
        "this_round_sleep_secs": round(this_round_sleep_secs) if this_round_sleep_secs is not None else None,
        "next_speak_earliest": round(next_speak_earliest),
        "next_speak_floor": next_speak_floor,
        "silence_remaining": silence_remaining,
        # reflection
        "reflection": reflection_out,
    }
