"""已加载人格的日摘要、用户画像和确定性事实索引维护。"""
import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from src.memory.curation import (
    DailyDialogue, curation_for, curation_preset, request_combined_suggestions, request_suggestions,
)
from src.memory.extraction_batches import plan_batch
from src.memory.fact_operations import choose_candidates
from src.memory.merged_history import memory_extraction_sources, memory_group_sources_allowed

logger = logging.getLogger(__name__)

_LAST_DAILY_RUN_FILE = "last_daily_run.json"


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
        return False
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


def _save_last_daily_run(storage_root: str, date_str: str) -> None:
    """写入本次每日任务运行日期。"""
    path = os.path.join(storage_root, _LAST_DAILY_RUN_FILE)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"last_daily_run_date": date_str}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("[daily_memory_job] 写入 last_daily_run 失败: %s", e)


async def run_day_summary_for_profile(profile_id: str, date_str: str, app) -> bool:
    """对指定人格运行指定日期的日摘要（已有摘要则跳过，无对话时生成安静日记）。返回是否执行了摘要。"""
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

    store = session.conversation_store
    has_conv = _has_conversation_on_date(store, date_str)

    mgr = _get_manager_for_session(session, app)
    if not mgr or not mgr._cfg.get("day_summary_enabled"):
        return False
    if mgr.day_store.has_summary_for(date_str):
        return False

    from src.config.profile_loader import ProfileLoader
    try:
        profile = ProfileLoader().load(profile_id)
    except Exception:
        profile = {}
    try:
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
    "maintenance_disabled": "每日记忆维护未启用",
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
    if not mgr._cfg.get("daily_forgetting_enabled"):
        return None, "maintenance_disabled"
    return mgr, None


def preview_forgetting_for_profile(
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


async def run_forgetting_for_profile(
    profile_id: str, app, yesterday: Optional[str] = None, *, force: bool = False
) -> Optional[Dict[str, Any]]:
    """只执行事实索引维护。保留原有调用参数，手动和定时执行使用同一开关。"""
    mgr, reason = _get_maintenance_manager(profile_id, app)
    if mgr is None:
        if force:
            logger.info("[daily_memory_job] 跳过手动记忆维护 profile=%s reason=%s", profile_id, reason)
        return None

    counts = await mgr.maintain_fact_index()
    logger.info(
        "[daily_memory_job] 记忆维护完成 profile=%s trigger=%s counts=%s",
        profile_id, "manual" if force else "scheduled", counts,
    )
    return counts


async def run_portrait_refresh_for_profile(profile_id: str, app, *, force: bool = False) -> None:
    """日批中调用 user_portrait 刷新（受 user_portrait_config 控制，完全独立于 memory 总开关）。"""
    sm = getattr(app.state, "session_manager", None)
    if not sm:
        return
    session = None
    for s in sm.list_sessions():
        if s.profile_id == profile_id:
            session = s
            break
    if not session:
        logger.debug("[daily_memory_job] portrait: profile %s 未加载，跳过", profile_id)
        return

    from src.config.profile_loader import ProfileLoader
    try:
        card = ProfileLoader().load(profile_id)
    except Exception:
        return
    cfg = card.get("user_portrait_config") or {}
    if not cfg.get("enabled", True):
        return
    if not force and not cfg.get("auto_refresh_in_daily_job", True):
        return

    from src.core.user_portrait import trigger_portrait_refresh
    try:
        await trigger_portrait_refresh(
            profile_id, session.storage_root, app,
            force=force, triggered_by="manual" if force else "daily",
        )
    except Exception as e:
        logger.exception("[daily_memory_job] portrait 刷新失败 profile=%s: %s", profile_id, e)


async def _run_daily_curation(session, today: str, app) -> None:
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

        async def decide(rows):
            return await request_suggestions(rows, preset)

        async def decide_combined(rows, dialogue):
            messages = manager._extraction_messages(dialogue.rows, profile, app, dialogue.candidates)
            return await request_combined_suggestions(rows, messages, preset)

        # Serialize with ordinary extraction so the same dialogue is not billed twice.
        async with manager._extraction_lock:
            if active_manager() is not manager:
                return
            preset = curation_preset(manager, app)
            changed = await curation_for(manager).run_daily(
                today, decide, enabled=lambda: active_manager() is manager,
                prepare_dialogue=prepare_dialogue, decide_combined=decide_combined,
            )
        if changed and manager.vectors and manager.vectors.is_available():
            for fact_id in changed:
                await manager.reembed_fact(fact_id)
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
    if _load_last_daily_run(session.storage_root) == today_str:
        logger.debug("[daily_memory_job] profile %s 今天已跑过每日任务，跳过", profile_id)
        return

    await _run_daily_curation(session, today_str, app)
    await run_day_summary_for_profile(profile_id, yesterday, app)

    # 画像有独立开关。索引维护失败也不影响当日的画像任务。
    await run_portrait_refresh_for_profile(profile_id, app)

    index_counts = await run_forgetting_for_profile(profile_id, app)
    if index_counts is None:
        manager = _get_manager_for_session(session, app)
        if manager and manager._cfg.get("enabled"):
            await manager.prune_curation_index()
    _save_last_daily_run(session.storage_root, today_str)


async def _catch_up_missed_runs(app) -> None:
    """启动补跑：桌面应用在 00:05 常处于关机状态，错过的每日任务在启动时补一次。
    依据 last_daily_run.json（每人格独立），今天没跑过的才补。"""
    sm = getattr(app.state, "session_manager", None)
    if not sm:
        return
    today_str = date.today().strftime("%Y-%m-%d")
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    for s in sm.list_sessions():
        if _load_last_daily_run(s.storage_root) == today_str:
            continue
        logger.info("[daily_memory_job] 启动补跑每日任务 profile=%s", s.profile_id)
        try:
            await run_daily_for_profile(s.profile_id, yesterday, app)
        except Exception as e:
            logger.exception("[daily_memory_job] 补跑 profile %s 失败: %s", s.profile_id, e)


async def run_daily_memory_loop(app) -> None:
    """每日任务主循环：sleep 到下一日 00:05（或可配置时刻），遍历已加载的 session 执行任务。
    启动 90 秒后先做一次补跑（见 _catch_up_missed_runs）。"""
    logger.info("[daily_memory_job] 每日记忆任务循环已启动")
    try:
        await asyncio.sleep(90)  # 避开启动高峰（TTS 预热、引擎启动）
        await _catch_up_missed_runs(app)
    except asyncio.CancelledError:
        logger.info("[daily_memory_job] 每日循环已取消")
        return
    except Exception as e:
        logger.exception("[daily_memory_job] 启动补跑异常: %s", e)
    while True:
        try:
            hour = 0
            minute = 5
            config = getattr(app.state, "config", None)
            if config:
                mem = getattr(config, "get_memory_config", lambda: {})()
                hour = int(mem.get("daily_run_at_hour", 0))
                minute = int(mem.get("daily_run_at_minute", 5))
            now = datetime.now()
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            wait_secs = (next_run - now).total_seconds()
            logger.info("[daily_memory_job] 下次运行: %s (%.0f 秒后)", next_run.isoformat(), wait_secs)
            await asyncio.sleep(wait_secs)

            yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
            sm = getattr(app.state, "session_manager", None)
            if not sm:
                await asyncio.sleep(60)
                continue
            for s in sm.list_sessions():
                try:
                    await run_daily_for_profile(s.profile_id, yesterday, app)
                except Exception as e:
                    logger.exception("[daily_memory_job] profile %s 失败: %s", s.profile_id, e)
        except asyncio.CancelledError:
            logger.info("[daily_memory_job] 每日循环已取消")
            break
        except Exception as e:
            logger.exception("[daily_memory_job] 循环异常: %s", e)
            await asyncio.sleep(60)
