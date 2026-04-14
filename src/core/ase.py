"""ase.py — Active Speaking Engine (ASE)

Reads session.reflection_state.urgency and decides when the AI should
proactively send a message without any user input.

Key design principles:
    - Single cooldown concept: urgency-weighted (higher urgency = shorter wait)
    - Mode system: 5 predefined profiles (low/medium/high/game/focus)
    - No conditional branches on mode in the engine code — just reads mode params
    - heartbeat_speak() uses build_messages(); scene context goes into user message
    - Only saves the assistant response (no artificial user message in history)
    - Timer expiry NOT handled here — it has its own path via system_trigger

Cooldown formula:
    actual_cooldown = min_interval × (1 - urgency × 0.85)
    e.g., urgency=1.0 → cooldown=0; urgency=threshold → cooldown≈min_interval×0.7

All operations are best-effort: exceptions never crash the loop.
"""
import asyncio
import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional

from src.config.prompt_loader import get_prompt, get_locale, get_raw, render

# Import ASE segments to trigger @register
import src.prompt.segments.ase.morning_greeting      # noqa: F401
import src.prompt.segments.ase.long_silence_check_in # noqa: F401

logger = logging.getLogger(__name__)

_ASE_STATE_FILE = "ase_state.json"


def _ase_state_path(storage_root: str) -> str:
    return os.path.join(storage_root, _ASE_STATE_FILE)


def _load_ase_state(storage_root: str) -> Dict[str, Any]:
    """Load persisted ASE state from disk; return empty dict on missing/error."""
    path = _ase_state_path(storage_root)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("[ASE] load_ase_state failed (%s): %s", path, e)
        return {}


def _save_ase_state(storage_root: str, state: Dict[str, Any]) -> None:
    """Persist ASE state to disk (best-effort)."""
    path = _ase_state_path(storage_root)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("[ASE] save_ase_state failed (%s): %s", path, e)


def _get_reflection_config_for_profile(profile_id: str) -> Dict[str, Any]:
    """Load segment_overrides and custom_segments from profile reflection_config."""
    try:
        from src.config.effective_config import _load_profile_card
        card = _load_profile_card(profile_id)
        ref = card.get("reflection_config") or {}
        return {
            "segment_overrides": ref.get("segment_overrides") or {},
            "custom_segments": ref.get("custom_segments") or [],
        }
    except Exception:
        return {"segment_overrides": {}, "custom_segments": []}


def _ase_segment_is_triggered(
    trigger_mode: str,
    trigger_param: float,
    hour: int,
    silent_seconds: float,
    hint_last_used: Dict[str, Any],
    segment_id: str,
    ase_cfg: Dict[str, Any],
    last_user_ts: Optional[float] = None,
) -> bool:
    """与人格 Prompt 段落一致的触发判断，用于 ASE 注入。"""
    from datetime import datetime
    now = time.time()
    today_str = datetime.now().strftime("%Y-%m-%d")
    last = hint_last_used.get(segment_id)

    if trigger_mode == "always":
        return True
    if trigger_mode == "probability":
        return random.random() < float(trigger_param)
    if trigger_mode == "cooldown":
        if last is None:
            return True
        if isinstance(last, (int, float)):
            return (now - last) / 60.0 >= float(trigger_param)
        return True
    if trigger_mode == "once_per_day":
        if last is None:
            return True
        if isinstance(last, (int, float)):
            return datetime.fromtimestamp(last).strftime("%Y-%m-%d") != today_str
        return last != today_str
    if trigger_mode == "first_after_silence":
        # 沉默后首条：仅在本轮沉默期内第一次 ASE 发言时触发（用户最后发言后沉默≥N分钟，且该段尚未在本轮触发过）
        if last_user_ts is None:
            return False
        silence_min = float(trigger_param)
        if silent_seconds < silence_min * 60:
            return False
        if last is None:
            return True
        if isinstance(last, (int, float)):
            return last < last_user_ts
        return True
    if trigger_mode == "time_window":
        start = int(trigger_param) // 100
        end = int(trigger_param) % 100
        if start <= end:
            return start <= hour <= end
        return hour >= start or hour <= end
    if trigger_mode in ("every_n_turns", "first_turn_only"):
        return True
    return True


def _build_ase_segment_list(profile_id: str) -> List[Dict[str, Any]]:
    """Build ASE injection list from unified segment registry + segments_config."""
    from src.prompt.registry import get_registered
    from src.prompt.base import targets_ase
    from src.prompt.segment_config import load_segment_config

    seg_cfg = load_segment_config(profile_id)
    out = []

    for seg_cls in get_registered():
        if not targets_ase(seg_cls.inject_into):
            continue
        seg_id = seg_cls.segment_id
        meta = seg_cfg.get_meta(seg_id)
        if meta is not None and meta.enabled is False:
            continue
        # Content: use meta.content override, else DEFAULT_CONTENT from class
        content = ""
        if meta and meta.content:
            content = meta.content.strip()
        if not content:
            content = getattr(seg_cls, "DEFAULT_CONTENT", "").strip()
        if not content:
            continue
        trigger_mode = (meta.trigger_mode if meta and meta.trigger_mode else None) or seg_cls.default_trigger_mode
        trigger_param = (meta.trigger_param if meta and meta.trigger_param is not None else None)
        if trigger_param is None:
            trigger_param = seg_cls.default_trigger_param
        priority = (meta.priority if meta and meta.priority is not None else seg_cls.priority)
        out.append({
            "segment_id": seg_id,
            "content": content,
            "priority": priority,
            "trigger_mode": trigger_mode,
            "trigger_param": float(trigger_param),
        })

    # Custom segments targeting ase
    for c in seg_cfg.custom_segments:
        if not targets_ase(c.inject_into):
            continue
        content = (c.content or "").strip()
        if not content:
            continue
        out.append({
            "segment_id": c.segment_id,
            "content": content,
            "priority": c.priority,
            "trigger_mode": c.trigger_mode,
            "trigger_param": float(c.trigger_param),
        })
    return out


def _compute_ase_segment_injections(
    silent_seconds: float,
    profile_id: str,
    hint_last_used: Dict[str, Any],
    ase_cfg: Dict[str, Any],
    last_user_ts: Optional[float] = None,
) -> tuple:
    """按统一 trigger 规则计算本次要注入的段落内容与 used_ids。"""
    from datetime import datetime
    hour = datetime.now().hour
    segment_list = _build_ase_segment_list(profile_id)
    segment_list.sort(key=lambda x: x.get("priority", 50))
    texts = []
    used_ids = []
    for seg in segment_list:
        seg_id = seg["segment_id"]
        if not seg_id:
            continue
        triggered = _ase_segment_is_triggered(
            seg.get("trigger_mode", "always"),
            seg.get("trigger_param", 1.0),
            hour,
            silent_seconds,
            hint_last_used,
            seg_id,
            ase_cfg,
            last_user_ts=last_user_ts,
        )
        if not triggered:
            continue
        texts.append(seg["content"])
        used_ids.append(seg_id)
    return texts, used_ids


def _hint_last_used_after_speak(
    hint_last_used: Dict[str, Any],
    used_ids: List[str],
) -> Dict[str, Any]:
    """Return updated hint_last_used after injecting (存时间戳，供 cooldown/once_per_day 使用)."""
    result = dict(hint_last_used)
    now = time.time()
    for seg_id in used_ids:
        result[seg_id] = now
    return result


def get_ase_context_for_reflection(storage_root: str) -> Dict[str, Any]:
    """Return read-only ASE context for ReflectionEngine (last speak time/content, optional counts)."""
    state = _load_ase_state(storage_root)
    last_speak_time = state.get("last_speak_time")
    now = time.time()
    seconds_since_last_speak = (now - last_speak_time) if last_speak_time else None
    timestamps_24h = state.get("timestamps_24h", [])
    cutoff = now - 86400
    count_24h = len([t for t in timestamps_24h if t > cutoff])
    return {
        "last_speak_time": last_speak_time,
        "seconds_since_last_speak": seconds_since_last_speak,
        "last_speak_content": (state.get("last_speak_content") or "").strip(),
        "consecutive_speaks": state.get("consecutive_speaks", 0),
        "count_24h": count_24h,
    }


def _get_time_ctx(hour: int, locale: str) -> str:
    """Return locale-aware time period label for the given hour."""
    periods = get_raw("ase.time_periods") or {}
    mapping = [
        (range(5, 9),  "dawn"),
        (range(9, 12), "morning"),
        (range(12, 14), "midday"),
        (range(14, 18), "afternoon"),
        (range(18, 22), "evening"),
    ]
    for hr_range, key in mapping:
        if hour in hr_range:
            entry = periods.get(key, {})
            return entry.get(locale) or entry.get("zh") or key
    entry = periods.get("night", {})
    return entry.get(locale) or entry.get("zh") or ("night" if locale == "en" else "深夜")



class AseEngine:
    """Background asyncio task that periodically checks if ASE should speak."""

    def __init__(self, app):
        self._app = app
        # Per-profile runtime state
        # {profile_id: {"last_speak_time": float, "timestamps_24h": [float, ...]}}
        self._state: Dict[str, Dict[str, Any]] = {}
        self._tasks: Dict[str, asyncio.Task] = {}  # one loop task per session

    async def start(self):
        """Start per-session check loops for all loaded sessions."""
        sm = self._app.state.session_manager
        for session in sm.list_sessions():
            # Restore persisted ASE state (24h timestamps, last speak time, etc.)
            saved = _load_ase_state(session.storage_root)
            if saved:
                self._state[session.profile_id] = saved
                logger.debug("[ASE] restored state for profile=%s", session.profile_id)
            self._start_session_loop(session)
        ase_cfg = self._app.state.config.get_ase_config()
        logger.info(
            "[ASE] engine started for %d session(s) (mode=%s)",
            len(sm.list_sessions()),
            ase_cfg.get("mode", "medium"),
        )

    def _start_session_loop(self, session):
        """Start a dedicated check loop for one session."""
        sid = session.id
        if sid in self._tasks and not self._tasks[sid].done():
            return
        task = asyncio.create_task(
            self._session_loop(session),
            name=f"ase_{sid}",
        )
        self._tasks[sid] = task

    async def stop(self):
        """Cancel all running loops."""
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._tasks.clear()
        logger.info("[ASE] engine stopped")

    async def stop_session(self, session_id: str):
        """Cancel the per-session ASE loop (best-effort)."""
        try:
            task = self._tasks.get(session_id)
        except Exception:
            task = None
        if not task:
            return
        if task.done():
            # Ensure removal even if already done.
            self._tasks.pop(session_id, None)
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._tasks.pop(session_id, None)

    async def _session_loop(self, session):
        """Per-session check loop."""
        app = self._app

        # Initial delay to stagger sessions and let reflection run first
        await asyncio.sleep(30)

        while True:
            try:
                ase_cfg = app.state.config.get_ase_config()

                if not ase_cfg.get("enabled"):
                    logger.debug("[ASE] disabled, sleeping 60s...")
                    await asyncio.sleep(60)
                    continue

                mode_name = getattr(session, "ase_mode", "medium")
                # Fall back to config default if session hasn't been explicitly set
                if mode_name == "medium":
                    mode_name = ase_cfg.get("mode", "medium")

                modes = ase_cfg.get("modes", {})
                mode_cfg = modes.get(mode_name, modes.get("medium", {}))

                check_interval = int(mode_cfg.get("check_interval", 300))

                # 只对当前活跃 session 检查主动发言，切换走后立即静止
                current = app.state.session_manager.get_current()
                if not current or current.id != session.id:
                    await asyncio.sleep(check_interval)
                    continue
                # 人格级别覆盖：该人格单独关闭了 ASE 则跳过
                try:
                    from src.config.effective_config import get_effective_engine_config
                    eff = get_effective_engine_config(app, session.profile_id, "ase")
                    if not eff.get("enabled", True):
                        await asyncio.sleep(check_interval)
                        continue
                except Exception:
                    pass

                # Track last check time for UI next_check_in display (in-memory only)
                profile_state = self._state.setdefault(session.profile_id, {})
                now = time.time()
                profile_state["last_check_time"] = now
                await self._check_speak(session, ase_cfg, mode_name, mode_cfg)

                # 自适应睡眠：在「可能可以发言」的时间点附近醒来，其余时间睡满 check_interval
                global_last = getattr(app.state, "last_user_message_time", 0.0)
                effective_last = max(session.last_user_message_time, global_last)
                silent_seconds = now - effective_last
                min_silent = float(mode_cfg.get("min_silent_seconds", 300))
                silence_remaining = max(0.0, min_silent - silent_seconds)
                sleep_secs = min(check_interval, max(1.0, silence_remaining))
                profile_state["next_check_at"] = time.time() + sleep_secs
                profile_state["this_round_sleep_secs"] = sleep_secs
                _save_ase_state(session.storage_root, profile_state)
                await asyncio.sleep(sleep_secs)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[ASE] session=%s loop error: %s", session.id, e)
                try:
                    await asyncio.sleep(check_interval)
                except NameError:
                    await asyncio.sleep(300)

    async def _check_speak(
        self,
        session,
        ase_cfg: Dict,
        mode_name: str,
        mode_cfg: Dict,
    ):
        """Decide whether to speak proactively for this session."""
        from src.utils.debug_logger import log_ase_check

        # ── Always-first: reset consecutive count if user replied since last speak ──
        # Must run before any gate so the reset persists even when gates block.
        profile_state = self._state.setdefault(session.profile_id, {})
        last_speak_t = profile_state.get("last_speak_time", 0)
        if session.last_user_message_time > last_speak_t and profile_state.get("consecutive_speaks", 0) > 0:
            profile_state["consecutive_speaks"] = 0
            _save_ase_state(session.storage_root, profile_state)
            logger.debug("[ASE] consecutive_speaks reset (user replied) session=%s", session.id)

        reflection_state = getattr(session, "reflection_state", None)
        urgency = reflection_state.get("urgency", 0.0) if reflection_state else 0.0
        threshold = float(mode_cfg.get("urgency_threshold", 0.35))
        min_silent = float(mode_cfg.get("min_silent_seconds", 300))
        # 跨 session 沉默检测：取当前 session 与全局最后活动时间的较大值
        # 防止用户切换人物卡后，旧 session 误以为用户沉默而触发主动发言
        global_last = getattr(self._app.state, "last_user_message_time", 0.0)
        effective_last = max(session.last_user_message_time, global_last)
        silent_seconds = time.time() - effective_last
        cooldown_required = 0.0
        cooldown_elapsed = 0.0

        # ── Gate 1: urgency below threshold ──────────────────────────────────
        if not reflection_state or urgency < threshold:
            log_ase_check(
                session_id=session.id,
                mode=mode_name,
                urgency=urgency,
                threshold=threshold,
                silent_seconds=silent_seconds,
                decision="skip",
                skip_reason="low_urgency",
            )
            return

        # ── Gate 2: user must be silent long enough ───────────────────────────
        if silent_seconds < min_silent:
            log_ase_check(
                session_id=session.id,
                mode=mode_name,
                urgency=urgency,
                threshold=threshold,
                silent_seconds=silent_seconds,
                decision="skip",
                skip_reason="not_silent",
            )
            return

        # ── Gate 3: cooldown check ────────────────────────────────────────────
        last_speak = profile_state.get("last_speak_time")
        min_interval = float(mode_cfg.get("min_interval", 600))

        if last_speak:
            cooldown_elapsed = time.time() - last_speak
            # urgency=1.0 → cooldown=0; urgency=threshold → cooldown≈min_interval×0.7
            cooldown_required = min_interval * (1.0 - urgency * 0.85)
            if cooldown_elapsed < cooldown_required:
                log_ase_check(
                    session_id=session.id,
                    mode=mode_name,
                    urgency=urgency,
                    threshold=threshold,
                    silent_seconds=silent_seconds,
                    decision="skip",
                    skip_reason="cooldown",
                    cooldown_required=cooldown_required,
                    cooldown_elapsed=cooldown_elapsed,
                )
                return

        # ── Gate 4: 24h hard cap ──────────────────────────────────────────────
        max_24h = int(mode_cfg.get("max_per_24h", 15))
        if self._count_24h(session.profile_id) >= max_24h:
            log_ase_check(
                session_id=session.id,
                mode=mode_name,
                urgency=urgency,
                threshold=threshold,
                silent_seconds=silent_seconds,
                decision="skip",
                skip_reason="24h_limit",
            )
            return

        # ── Gate 5: consecutive speaks without user response ──────────────────
        max_consec = mode_cfg.get("max_consecutive_without_response")
        if max_consec is not None and int(max_consec) > 0:
            consecutive = profile_state.get("consecutive_speaks", 0)
            if consecutive >= int(max_consec):
                log_ase_check(
                    session_id=session.id,
                    mode=mode_name,
                    urgency=urgency,
                    threshold=threshold,
                    silent_seconds=silent_seconds,
                    decision="skip",
                    skip_reason="max_consecutive",
                )
                return

        # ── Gate 6: 不与正常对话回复同时进行，避免两条回复合并成一条 ─────────
        streaming_set = getattr(self._app.state, "chat_streaming_sessions", None)
        if streaming_set and session.id in streaming_set:
            log_ase_check(
                session_id=session.id,
                mode=mode_name,
                urgency=urgency,
                threshold=threshold,
                silent_seconds=silent_seconds,
                decision="skip",
                skip_reason="chat_streaming",
            )
            return

        # ── All gates passed — log and speak ─────────────────────────────────
        log_ase_check(
            session_id=session.id,
            mode=mode_name,
            urgency=urgency,
            threshold=threshold,
            silent_seconds=silent_seconds,
            decision="speak",
            skip_reason="",
            cooldown_required=cooldown_required,
            cooldown_elapsed=cooldown_elapsed,
        )

        await self._speak(session, reflection_state, mode_name, mode_cfg)

    async def _speak(
        self,
        session,
        reflection_state: Dict,
        mode_name: str,
        mode_cfg: Dict,
    ):
        """Execute heartbeat_speak: build messages, call LLM, broadcast, save."""
        from src.core.broadcast import broadcast
        from src.prompt.pipeline import build_messages
        from src.llm.registry import get_provider
        from src.utils.debug_logger import log_ase_speak, log_ase_error, log_llm_call, log_llm_response

        app = self._app
        config = app.state.config
        ase_cfg = config.get_ase_config()
        vlm_cfg = config.get_vlm_config()

        # ── Optional VLM screenshot ───────────────────────────────────────────
        vlm_description = ""
        vlm_mode = ase_cfg.get("vlm_mode", "random")
        use_vlm = (
            vlm_cfg.get("enabled")
            and vlm_cfg.get("for_ase")
            and (
                vlm_mode == "always"
                or (vlm_mode == "random" and random.random() < 0.4)
            )
        )

        if use_vlm:
            vlm_description = await self._request_screenshot_and_describe(session)

        # ── Message config (game mode can override) ───────────────────────────
        from src.config.effective_config import get_effective_max_history_turns
        msg_cfg = mode_cfg.get("message_config") or {}
        n_history = int(msg_cfg.get("n_history_turns") or get_effective_max_history_turns(app, session.profile_id))
        max_facts = msg_cfg.get("max_facts_in_prompt")  # None = use global default
        extra_behavior = (msg_cfg.get("extra_behavior") or "").strip()
        if not extra_behavior:
            from src.config.prompt_loader import get_dict, get_locale
            _eb_section = get_dict(f"ase.mode_extra_behavior", locale=get_locale())
            extra_behavior = (_eb_section.get(mode_name) or "").strip()

        # ── Heartbeat context injected into system message ────────────────────
        reflection_state["_check_time"] = time.time()
        profile_state = self._state.get(session.profile_id, {})
        last_ase_content = (profile_state.get("last_speak_content") or "").strip()
        last_speak_time = profile_state.get("last_speak_time")
        last_user_ts = getattr(session, "last_user_message_time", None)
        if last_user_ts is None and hasattr(session, "conversation_store") and session.conversation_store:
            user_name = getattr(getattr(self._app, "state", None) and getattr(self._app.state, "config", None), "user_name", "用户") or "用户"
            last_user_ts = session.conversation_store.last_user_message_time(user_name)
        if last_speak_time is not None and last_user_ts is not None and last_user_ts > last_speak_time:
            last_ase_content = ""

        from datetime import datetime as _dt
        now = time.time()
        ref_cfg = _get_reflection_config_for_profile(session.profile_id)
        global_last = getattr(app.state, "last_user_message_time", 0.0)
        effective_last = max(last_user_ts or 0, global_last)
        silent_seconds = now - effective_last

        # 情境段落（内置 + 自定义，统一按 trigger_mode/trigger_param 与人格 Prompt 一致）
        ase_state = _load_ase_state(session.storage_root)
        hint_last_used = ase_state.get("hint_last_used") or {}
        last_user_ts_float = (last_user_ts if isinstance(last_user_ts, (int, float)) else None)
        segment_texts, injected_builtin_ids = _compute_ase_segment_injections(
            silent_seconds, session.profile_id, hint_last_used, ase_cfg,
            last_user_ts=last_user_ts_float,
        )
        greeting_hint = "\n".join(segment_texts) if segment_texts else ""

        # extra_system: only per-mode extra_behavior (behavioral guidance is now a segment)
        extra_system = extra_behavior or ""

        # ── Build messages ────────────────────────────────────────────────────
        store = session.conversation_store

        # Ensure MemoryManager exists so memory segments can access it
        try:
            managers = getattr(app.state, "memory_managers", None)
            if managers is None:
                app.state.memory_managers = {}
                managers = app.state.memory_managers
            if session.profile_id not in managers:
                from src.memory.memory_manager import MemoryManager
                mem_cfg = config.get_memory_config()
                managers[session.profile_id] = MemoryManager(session.profile_id, mem_cfg)
        except Exception:
            pass

        preset = config.get_active_llm_preset()
        if not preset.get("api_key") and not preset.get("base_url"):
            logger.warning("[ASE] LLM not configured, skipping speak")
            return

        messages = build_messages(
            session,
            "",  # empty user_msg — scene context injected via ase_scene_context segment
            store,
            n_history_turns=n_history,
            preset=preset,
            app=app,
            extra_system=extra_system,
            max_facts_override=max_facts,
            pipeline_mode="ase",
            extra_context={
                "ase_vlm_description": vlm_description,
                "ase_last_content": last_ase_content,
                "ase_greeting_hint": greeting_hint,
            },
        )

        # Remove empty user messages (if pipeline produced any before scene context)
        messages = [
            m for m in messages
            if not (m["role"] == "user" and not m["content"].strip())
        ]

        llm = get_provider(preset)
        gen_kwargs = {
            k: preset.get(k)
            for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty")
            if preset.get(k) is not None
        }
        log_llm_call(
            messages=messages,
            model=preset.get("model", ""),
            gen_kwargs=gen_kwargs,
            session_id=session.id,
        )

        # ── Stream generation + broadcast ─────────────────────────────────────
        t0 = time.time()
        full_response = ""
        vlm_used = bool(vlm_description)
        try:
            async for token in llm.stream_chat(messages):
                full_response += token
                await broadcast.push(session.id, {"token": token, "done": False})

            full_response = full_response.replace("**", "").strip()

            # Save only the assistant response (no user trigger message)
            store.append("assistant", full_response, sender=session.display_name or "")

            await broadcast.push(session.id, {
                "type": "new_message",
                "role": "assistant",
                "content": full_response,
                "timestamp": time.time(),
                "client_id": "",
                "sender": session.display_name or "",
            })
            await broadcast.push(session.id, {"token": "", "done": True})

            duration_ms = int((time.time() - t0) * 1000)
            log_llm_response(
                session_id=session.id,
                response=full_response,
                model=preset.get("model", ""),
            )
            log_ase_speak(
                session_id=session.id,
                mode=mode_name,
                urgency=reflection_state.get("urgency", 0.0),
                vlm_used=vlm_used,
                vlm_description_len=len(vlm_description),
                response_chars=len(full_response),
                duration_ms=duration_ms,
            )
            logger.info(
                "[ASE] session=%s spoke (%d chars, vlm=%s)",
                session.id, len(full_response), vlm_used
            )

            # ── Update ASE state and persist ───────────────────────────────────
            state = self._state.setdefault(session.profile_id, {})
            state["last_speak_time"] = time.time()
            state["last_speak_content"] = (full_response[:250] or "").strip()
            state.setdefault("timestamps_24h", []).append(time.time())
            state["consecutive_speaks"] = state.get("consecutive_speaks", 0) + 1
            if injected_builtin_ids:
                state["hint_last_used"] = _hint_last_used_after_speak(
                    state.get("hint_last_used") or {},
                    injected_builtin_ids,
                )
            # Write proactive_log entry for topic deduplication in reflection
            speak_reason = reflection_state.get("speak_reason", "none") if reflection_state else "none"
            topic_anchor = reflection_state.get("topic_anchor", "") if reflection_state else ""
            log_entry = {
                "timestamp": time.time(),
                "topic_anchor": topic_anchor[:80],
                "speak_reason": speak_reason,
                "urgency": reflection_state.get("urgency", 0.0) if reflection_state else 0.0,
            }
            proactive_log = state.get("proactive_log", [])
            proactive_log.append(log_entry)
            state["proactive_log"] = proactive_log[-10:]  # keep last 10
            _save_ase_state(session.storage_root, state)

            # Reset urgency so we don't speak again immediately
            if session.reflection_state:
                session.reflection_state["urgency"] = 0.0
                session.save_runtime_state()

            # Trigger TTS if enabled (fire-and-forget via broadcast already handles it)
            # The frontend SSE handler for "new_message" will initiate TTS if enabled

        except Exception as e:
            logger.error("[ASE] session=%s speak error: %s", session.id, e)
            log_ase_error(session_id=session.id, error=str(e))
            # Send done signal so frontend doesn't hang
            try:
                await broadcast.push(session.id, {"token": "", "done": True})
            except Exception:
                pass

    async def _request_screenshot_and_describe(self, session) -> str:
        """Request a screenshot from the frontend, wait, then call VLM."""
        from src.core.broadcast import broadcast

        app = self._app
        vlm_cfg = app.state.config.get_vlm_config()
        wait_secs = int(vlm_cfg.get("ase_wait_seconds", 3))

        # Clear old screenshot first
        session.last_screenshot_b64 = None

        # Ask frontend to take a screenshot
        await broadcast.push(session.id, {"type": "heartbeat_screenshot_request"})
        logger.debug("[ASE] sent heartbeat_screenshot_request to session=%s", session.id)

        # Wait for frontend to capture and upload
        await asyncio.sleep(wait_secs)

        b64 = getattr(session, "last_screenshot_b64", None)
        if not b64:
            logger.debug("[ASE] no screenshot received within %ds, proceeding without VLM", wait_secs)
            return ""

        # Describe the screenshot
        try:
            preset_name = vlm_cfg.get("model_preset", "Gemini-3.0")
            preset = app.state.config.get_llm_preset(preset_name)
            if not preset or not preset.get("api_key"):
                return ""

            from src.api.vlm import _call_vlm
            description = await _call_vlm(preset, b64, session_id=session.id)
            logger.debug("[ASE] VLM description: %s", description[:80])
            return description
        except Exception as e:
            logger.warning("[ASE] VLM call failed: %s", e)
            return ""

    def _count_24h(self, profile_id: str) -> int:
        """Count proactive speaks in the last 24 hours."""
        state = self._state.get(profile_id, {})
        timestamps = state.get("timestamps_24h", [])
        cutoff = time.time() - 86400
        # Prune old timestamps in-place
        recent = [t for t in timestamps if t > cutoff]
        state["timestamps_24h"] = recent
        return len(recent)
