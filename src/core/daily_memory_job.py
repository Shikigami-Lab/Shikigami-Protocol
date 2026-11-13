"""Independent daily maintenance and scheduled model memory work."""
import asyncio
import json
import logging
import os
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Optional

from src.memory.batch_curation import review_options
from src.memory.curation import (
    CurationConflict, DailyDialogue, curation_for, curation_preset, request_batch_review, request_daily_extraction,
    review_model_key,
)
from src.memory.extraction_batches import plan_batch
from src.memory.fact_operations import choose_candidates
from src.memory.merged_history import memory_extraction_sources, memory_group_sources_allowed

logger = logging.getLogger(__name__)

_LAST_DAILY_RUN_FILE = "last_daily_run.json"
_DAILY_PROGRESS_FILE = "daily_job_progress.json"
_DAILY_MAX_ATTEMPTS = 3
_DAILY_RETRY_SECONDS = 300
_LAST_CURATION_CHECK = "last_scheduled_check_date"


def _msg_date(msg) -> str:
    """从消息取日期 YYYY-MM-DD（与 chat._msg_date 一致）。"""
    ts = msg.get("timestamp") if isinstance(msg, dict) else getattr(msg, "timestamp", None)
    if ts is not None:
        try:
            return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
        except Exception:
            pass
    return ""


def _msg_role(msg) -> Optional[str]:
    """从消息取 role（兼容 dict 与 Message 等对象）。"""
    if isinstance(msg, dict):
        return msg.get("role")
    return getattr(msg, "role", None)


def _has_conversation_on_date(store, date_str: str) -> bool:
    """该 store 在 date_str 当天是否有至少一条 user/assistant 消息。"""
    try:
        all_msgs = store.get_all()
    except Exception as e:
        logger.warning("[daily_memory_job] 读取对话失败: %s", e)
        raise
    return any(
        _msg_date(m) == date_str and _msg_role(m) in ("user", "assistant")
        for m in all_msgs
    )


def _get_manager_for_session(session, app) -> Optional[Any]:
    """为已加载的 session 获取或创建 MemoryManager，复用 app.state.memory_managers 缓存，
    确保每日任务写入的事实/向量与 API/UI 使用同一实例，避免任务写盘后界面仍显示旧数据。"""
    from src.api.memory import get_or_create_manager_for_session
    return get_or_create_manager_for_session(app, session)


def _load_last_daily_run(storage_root: str) -> Optional[str]:
    """读取上次每日任务运行日期 YYYY-MM-DD。"""
    path = os.path.join(storage_root, _LAST_DAILY_RUN_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("last_daily_run_date")
    except Exception:
        return None


def _atomic_daily_state(storage_root: str, filename: str, data: dict) -> None:
    path = os.path.join(storage_root, filename)
    tmp = path + ".tmp"
    os.makedirs(storage_root, exist_ok=True)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _save_last_daily_run(storage_root: str, date_str: str) -> None:
    _atomic_daily_state(storage_root, _LAST_DAILY_RUN_FILE, {"last_daily_run_date": date_str})


def _load_daily_progress(storage_root: str, day: str) -> dict:
    try:
        with open(os.path.join(storage_root, _DAILY_PROGRESS_FILE), encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict) and isinstance(state.get("day"), str) and isinstance(state.get("jobs"), dict):
            pending = [state["jobs"].get(name, {}) for name in ("summary", "portrait", "index")]
            retryable = any(item.get("status") != "completed"
                            and item.get("attempts", 0) < _DAILY_MAX_ATTEMPTS for item in pending)
            marker_pending = (all(item.get("status") == "completed" for item in pending)
                              and _load_last_daily_run(storage_root) != state["day"])
            # Midnight changes the scheduler's date, not the identity of work
            # still awaiting a retry or its final completion-marker write.
            if state["day"] == day or (state["day"] < day and (retryable or marker_pending)):
                if "target_date" not in state:
                    state["target_date"] = (date.fromisoformat(state["day"]) - timedelta(days=1)).isoformat()
                return state
    except (OSError, ValueError, TypeError):
        pass
    return {"day": day, "jobs": {}}


def _daily_retry_due(storage_root: str, now: datetime) -> bool:
    state = _load_daily_progress(storage_root, now.date().isoformat())
    if not state["jobs"] or _load_last_daily_run(storage_root) == state["day"]:
        return False
    pending = [state["jobs"].get(name, {}) for name in ("summary", "portrait", "index")]
    if all(item.get("status") == "completed" for item in pending):
        return True  # The final completion-marker write can also fail.
    return any(item.get("status") != "completed" and item.get("attempts", 0) < _DAILY_MAX_ATTEMPTS
               and item.get("retry_at", 0) <= now.timestamp() for item in pending)


async def run_day_summary_for_profile(profile_id: str, date_str: str, app) -> Optional[bool]:
    """Generate the diary: True completed, None deliberately skipped, False failed."""
    sm = getattr(app.state, "session_manager", None)
    if not sm:
        return False
    session = None
    for s in sm.list_sessions():
        if s.profile_id == profile_id:
            session = s
            break
    if not session:
        logger.debug("[daily_memory_job] profile %s 未加载，跳过日摘要", profile_id)
        return False

    mgr = _get_manager_for_session(session, app)
    if not mgr:
        return False
    if (not mgr._cfg.get("enabled") or not mgr._cfg.get("day_summary_enabled")
            or mgr._cfg.get("extraction_llm_preset") == "__none__"):
        return None
    if mgr.day_store.has_summary_for(date_str):
        return None

    store = session.conversation_store
    from src.config.profile_loader import ProfileLoader
    try:
        profile = ProfileLoader().load(profile_id)
    except Exception:
        profile = {}
    try:
        has_conv = _has_conversation_on_date(store, date_str)
        result = await mgr.generate_day_summary(date_str, store, profile, app, quiet=not has_conv)
        if result:
            logger.info("[daily_memory_job] 日摘要完成 profile=%s date=%s quiet=%s", profile_id, date_str, not has_conv)
        return bool(result)
    except Exception as e:
        logger.exception("[daily_memory_job] 日摘要失败 profile=%s date=%s: %s", profile_id, date_str, e)
        return False


_MAINTENANCE_SKIP_REASONS = {
    "session_manager_unavailable": "SessionManager 不可用",
    "profile_not_loaded": "人格未加载",
    "memory_manager_unavailable": "无法创建 MemoryManager",
    "memory_disabled": "记忆未启用",
}


def _get_maintenance_manager(profile_id: str, app):
    sm = getattr(app.state, "session_manager", None)
    if not sm:
        return None, "session_manager_unavailable"
    session = next((s for s in sm.list_sessions() if s.profile_id == profile_id), None)
    if session is None:
        return None, "profile_not_loaded"
    mgr = _get_manager_for_session(session, app)
    if mgr is None:
        return None, "memory_manager_unavailable"
    if not mgr._cfg.get("enabled"):
        return None, "memory_disabled"
    return mgr, None


def preview_fact_index_maintenance(
    profile_id: str, app, yesterday: Optional[str] = None
) -> Dict[str, Any]:
    """预览当前事实到期和向量清理数量。保留旧预览字段以兼容现有客户端。"""
    mgr, reason = _get_maintenance_manager(profile_id, app)
    out: Dict[str, Any] = {
        "profile_id": profile_id,
        "yesterday": yesterday or (date.today() - timedelta(days=1)).isoformat(),
        "skip": mgr is None,
        "expired_fact_count": 0,
        "orphan_cleanup": {"enabled": False, "would_delete_count": 0},
        "day_summary": {"enabled": False, "would_run": False, "reason": ""},
        "decay": {"enabled": False, "would_affect_count": 0},
        "reinforcement": {"enabled": False, "would_affect_count": 0},
        "consolidation": {
            "enabled": False,
            "would_run": False,
            "batch_size": 0,
            "candidates_count": 0,
            "total_facts": 0,
            "preview_facts": [],
        },
        "portrait_refresh": {
            "enabled": False,
            "would_run": False,
            "reason": "",
            "last_updated_at": 0,
            "refresh_count": 0,
        },
    }
    if mgr is None:
        out["reason_code"] = reason
        out["reason"] = _MAINTENANCE_SKIP_REASONS[reason]
        return out

    counts = mgr.preview_fact_index()
    out["expired_fact_count"] = counts["expired"]
    out["orphan_cleanup"] = {
        "enabled": mgr.fact_index is not None,
        "would_delete_count": counts["vectors_removed"],
    }
    return out


async def run_fact_index_maintenance(
    profile_id: str, app, yesterday: Optional[str] = None, *, force: bool = False
) -> Optional[Dict[str, Any]]:
    """Clean the index for a loaded character with memory enabled."""
    mgr, reason = _get_maintenance_manager(profile_id, app)
    if mgr is None:
        if force:
            logger.info("[daily_memory_job] 跳过手动索引清理 profile=%s reason=%s", profile_id, reason)
        return None

    counts = await mgr.maintain_fact_index()
    logger.info(
        "[daily_memory_job] 索引清理完成 profile=%s trigger=%s counts=%s",
        profile_id, "manual" if force else "scheduled", counts,
    )
    return counts


async def run_portrait_refresh_for_profile(profile_id: str, app, *, force: bool = False) -> Optional[bool]:
    """Independent portrait refresh: True completed, None skipped, False failed."""
    sm = getattr(app.state, "session_manager", None)
    if not sm:
        return False
    session = None
    for s in sm.list_sessions():
        if s.profile_id == profile_id:
            session = s
            break
    if not session:
        logger.debug("[daily_memory_job] portrait: profile %s 未加载，跳过", profile_id)
        return False

    from src.config.profile_loader import ProfileLoader
    try:
        card = ProfileLoader().load(profile_id)
    except Exception:
        return False
    cfg = card.get("user_portrait_config") or {}
    if not cfg.get("enabled", True):
        return None
    if not force and not cfg.get("auto_refresh_in_daily_job", True):
        return None

    from src.core import user_portrait as portrait
    from src.core.user_portrait import trigger_portrait_refresh
    # The legacy refresh API returns None for both skips and failures. Check its
    # non-model gates first so a failed request is never recorded as completed.
    current = card.get("user_portrait") or {}
    if not force and portrait._is_in_cooldown(current, float(cfg.get("min_hours_between_refresh", 6))):
        return None
    conversation = portrait._get_recent_conversation_text(
        session.storage_root, int(cfg.get("max_input_conv_turns", 60)), portrait._human_label(app))
    summaries = portrait._get_recent_day_summaries_text(session.storage_root, 7) if cfg.get("use_day_summary_as_input", True) else ""
    facts = portrait._get_facts_text(session.storage_root, 30) if cfg.get("use_facts_as_input", True) else ""
    content = (current.get("content") or "").strip()
    if not conversation and not summaries and not facts and not content:
        return None
    recent = current.get("recent_context") or {}
    recent = (recent.get("content") or "").strip() if isinstance(recent, dict) else str(recent).strip()
    signature = portrait._compute_input_signature(
        content, recent, json.dumps(current.get("contextual_details") or [], ensure_ascii=False),
        portrait._resolve_user_intro(card, app)[1], conversation, summaries, facts)
    if not force and signature == current.get("last_input_signature"):
        return None
    try:
        result = await trigger_portrait_refresh(
            profile_id, session.storage_root, app,
            force=force, triggered_by="manual" if force else "daily",
        )
        return bool(result)
    except Exception as e:
        logger.exception("[daily_memory_job] portrait 刷新失败 profile=%s: %s", profile_id, e)
        return False


def _curation_is_due(state, cfg, now: datetime) -> bool:
    interval = cfg.get("curation_interval_days", 1)
    if type(interval) is not int or not 1 <= interval <= 30:
        interval = 1

    daily = state.get("daily")
    legacy_day = daily.get("day") if isinstance(daily, dict) else None
    last_dates = []
    for value in (state.get(_LAST_CURATION_CHECK), legacy_day):
        try:
            last_dates.append(date.fromisoformat(value))
        except (TypeError, ValueError):
            continue
    if last_dates:
        elapsed = (now.date() - max(last_dates)).days
        if elapsed != interval:
            return elapsed > interval

    hour = cfg.get("curation_run_at_hour")
    minute = cfg.get("curation_run_at_minute")
    deadline = time(int(cfg.get("daily_run_at_hour", 0) if hour is None else hour),
                    int(cfg.get("daily_run_at_minute", 5) if minute is None else minute))
    return now.time() >= deadline


async def _run_scheduled_curation(session, app, *, now: Optional[datetime] = None) -> None:
    try:
        await _run_daily_curation(session, None, app, now=now)
    except Exception:
        logger.exception("Could not check the memory review schedule for profile %s", session.profile_id)


async def _run_daily_curation(session, today: Optional[str], app, *, now: Optional[datetime] = None) -> None:
    """Run the optional model task, recording a completed check when scheduled."""
    def active_manager():
        manager = _get_manager_for_session(session, app)
        if (manager and manager._cfg.get("enabled") and manager._cfg.get("daily_curation_enabled")
                and manager._cfg.get("extraction_llm_preset") != "__none__"):
            return manager
        return None

    manager = active_manager()
    if manager is None:
        return
    try:
        profile = {}

        def prepare_dialogue(facts, cursors, offsets):
            nonlocal profile
            if not cursors:
                return None
            from src.config.profile_loader import ProfileLoader

            profile = ProfileLoader().load(session.profile_id)
            sources = memory_extraction_sources(session, app, profile)
            streams = {
                source: [dict(row, source=source)
                         for row in store.memory_turn_batch_after(cursors[source], max_user_turns=20)]
                for source, store in sources.items() if source in cursors
            }
            rows, originals, next_cursors, next_offsets = plan_batch(streams, cursors, offsets)
            if not rows and next_cursors == cursors and next_offsets == offsets:
                return None

            group_rows = [row for row in originals if row["source"].startswith("group:")]

            def dialogue_is_current():
                if group_rows and (
                    getattr(app.state, "group_manager", None) is None
                    or not memory_group_sources_allowed(app, session.profile_id, group_rows)
                ):
                    return False
                return all(sources[row["source"]].has_memory_evidence(row) for row in originals)

            return DailyDialogue(
                rows=rows, candidates=choose_candidates(facts, rows),
                next_cursors=next_cursors, next_offsets=next_offsets, is_current=dialogue_is_current,
                threshold=float(manager._cfg.get("extraction_weight_threshold", 0.5)),
            )

        def review_is_current():
            if fixed_review_prompt(app) != review_prompt or review_options(manager._cfg) != (active_limit, batch_size):
                return False
            try:
                return review_model_key(curation_preset(manager, app), review_prompt=review_prompt) == model_key
            except CurationConflict:
                return False

        async def decide(batch):
            if active_manager() is not manager or not review_is_current():
                raise CurationConflict("Memory review settings changed before the request")
            return await request_batch_review(batch, preset, review_prompt=review_prompt)

        async def decide_dialogue(dialogue, facts):
            dialogue.candidates = await manager.choose_extraction_candidates(facts, dialogue.rows)
            if active_manager() is not manager or not dialogue.is_current():
                raise CurationConflict("Memory extraction changed before the request")
            messages = manager._extraction_messages(dialogue.rows, profile, app, dialogue.candidates)
            return await request_daily_extraction(messages, preset)

        # Serialize with ordinary extraction so the same dialogue is not billed twice.
        async with manager._extraction_lock:
            if active_manager() is not manager:
                return
            scheduled = today is None
            if scheduled:
                checked_at = now if now is not None else datetime.now()
                state = manager.facts.curation_snapshot()[-1]
                if not _curation_is_due(state, manager._cfg, checked_at):
                    return
                today = checked_at.date().isoformat()

            from src.memory.curation import fixed_review_prompt
            from src.memory.review_neighbors import find_review_comparison

            preset = curation_preset(manager, app)
            review_prompt = fixed_review_prompt(app)
            model_key = review_model_key(preset, review_prompt=review_prompt)
            active_limit, batch_size = review_options(manager._cfg)
            changed = await curation_for(manager).run_daily(
                today, decide, enabled=lambda: active_manager() is manager,
                prepare_dialogue=prepare_dialogue, decide_dialogue=decide_dialogue,
                review_is_current=review_is_current,
                review_limit=active_limit,
                review_batch_size=batch_size,
                review_comparison=lambda fact, facts: find_review_comparison(fact, facts, manager.vectors),
            )
            if scheduled and active_manager() is manager:
                revision, cursors, facts, state = manager.facts.curation_snapshot()
                state[_LAST_CURATION_CHECK] = today
                manager.facts.apply_curation(revision, facts, cursors, state)
        if changed and manager.vectors and manager.vectors.is_available():
            for fact_id in changed:
                await manager.reembed_fact(fact_id)
        if active_manager() is manager:
            await manager.prune_curation_index()
    except Exception:
        logger.warning("Daily curation unavailable for profile %s", session.profile_id)


async def run_daily_for_profile(profile_id: str, yesterday: str, app) -> None:
    """分别运行日摘要、画像和事实索引维护。同一天的启动补跑和定时运行共用标记。"""
    sm = getattr(app.state, "session_manager", None)
    if not sm:
        return
    session = None
    for s in sm.list_sessions():
        if s.profile_id == profile_id:
            session = s
            break
    if not session:
        return

    today_str = date.today().strftime("%Y-%m-%d")
    progress = _load_daily_progress(session.storage_root, today_str)
    run_day = progress["day"]
    if _load_last_daily_run(session.storage_root) == run_day:
        logger.debug("[daily_memory_job] profile %s 今天已跑过每日任务，跳过", profile_id)
        return

    target_date = progress.setdefault("target_date", yesterday)
    errors = []
    jobs = (
        ("summary", lambda: run_day_summary_for_profile(profile_id, target_date, app)),
        ("portrait", lambda: run_portrait_refresh_for_profile(profile_id, app)),
        ("index", lambda: run_fact_index_maintenance(profile_id, app)),
    )
    for name, run in jobs:
        previous = progress["jobs"].get(name, {})
        if previous.get("status") == "completed" or previous.get("attempts", 0) >= _DAILY_MAX_ATTEMPTS:
            continue
        attempts = previous.get("attempts", 0) + 1
        try:
            result = await run()
            completed = result is not False  # None is a deliberate disabled/no-work skip.
        except Exception as e:
            errors.append(e)
            completed = False
        progress["jobs"][name] = {
            "status": "completed" if completed else "failed", "attempts": attempts,
            "retry_at": datetime.now().timestamp() + _DAILY_RETRY_SECONDS if not completed else 0,
        }
        _atomic_daily_state(session.storage_root, _DAILY_PROGRESS_FILE, progress)
    if all(progress["jobs"].get(name, {}).get("status") == "completed" for name, _ in jobs):
        _save_last_daily_run(session.storage_root, run_day)
    if errors:
        raise errors[0]


async def _catch_up_missed_runs(app) -> None:
    """Check the index at startup and catch up today's scheduled jobs when needed."""
    sm = getattr(app.state, "session_manager", None)
    if not sm:
        return
    today_str = date.today().strftime("%Y-%m-%d")
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    for s in sm.list_sessions():
        try:
            if _load_last_daily_run(s.storage_root) == today_str:
                await run_fact_index_maintenance(s.profile_id, app)
            else:
                logger.info("[daily_memory_job] 启动补跑每日任务 profile=%s", s.profile_id)
                await run_daily_for_profile(s.profile_id, yesterday, app)
        except Exception as e:
            logger.exception("[daily_memory_job] 补跑 profile %s 失败: %s", s.profile_id, e)


async def _run_daily_tasks_loop(app) -> None:
    """Daily work plus three bounded attempts per job with a five-minute retry delay."""
    try:
        await _catch_up_missed_runs(app)
    except Exception as e:
        logger.exception("[daily_memory_job] Startup catch-up failed: %s", e)
    while True:
        try:
            now = datetime.now()
            config = getattr(app.state, "config", None)
            mem = getattr(config, "get_memory_config", lambda: {})() if config else {}
            deadline = now.replace(hour=int(mem.get("daily_run_at_hour", 0)),
                                   minute=int(mem.get("daily_run_at_minute", 5)), second=0, microsecond=0)
            sm = getattr(app.state, "session_manager", None)
            for session in sm.list_sessions() if sm else []:
                state = _load_daily_progress(session.storage_root, now.date().isoformat())
                if (_daily_retry_due(session.storage_root, now)
                        or (now >= deadline and not state["jobs"]
                            and _load_last_daily_run(session.storage_root) != now.date().isoformat())):
                    try:
                        await run_daily_for_profile(session.profile_id,
                                                    (now.date() - timedelta(days=1)).isoformat(), app)
                    except Exception:
                        logger.exception("[daily_memory_job] Profile task failed: %s", session.profile_id)
        except Exception:
            logger.exception("[daily_memory_job] Schedule check failed")
        await asyncio.sleep(60)


async def _run_curation_schedule_loop(app) -> None:
    """Check each loaded profile every minute without waiting on another profile's model."""
    running = {}
    try:
        while True:
            running = {profile_id: task for profile_id, task in running.items() if not task.done()}
            try:
                sm = getattr(app.state, "session_manager", None)
                if sm:
                    for session in sm.list_sessions():
                        if session.profile_id not in running:
                            running[session.profile_id] = asyncio.create_task(_run_scheduled_curation(session, app))
            except Exception:
                logger.exception("Could not enumerate scheduled memory reviews")
            now = datetime.now()
            await asyncio.sleep(60 - now.second - now.microsecond / 1_000_000)
    finally:
        for task in running.values():
            task.cancel()
        await asyncio.gather(*running.values(), return_exceptions=True)


async def run_daily_memory_loop(app) -> None:
    """Start the two memory schedules after the shared startup warmup."""
    logger.info("[daily_memory_job] 每日记忆任务循环已启动")
    try:
        await asyncio.sleep(90)
        await asyncio.gather(_run_daily_tasks_loop(app), _run_curation_schedule_loop(app))
    except asyncio.CancelledError:
        logger.info("[daily_memory_job] 每日循环已取消")
