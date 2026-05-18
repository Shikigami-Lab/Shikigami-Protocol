"""每日记忆/遗忘任务：与 day summary 同属一套，先日摘要再遗忘（衰减、孤儿清理、可选强化与合并）。

只跑已加载的 session；若某人格昨日完全没有对话则对该人格什么也不做。
"""
import asyncio
import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

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
    """对指定人格运行指定日期的日摘要（若已有摘要或当日无对话则跳过）。返回是否执行了摘要。"""
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


def _run_weight_decay(mgr, cfg: Dict[str, Any], yesterday: str) -> List[Dict[str, Any]]:
    """对非 pinned 且昨日未被使用的 fact 做 weight 衰减。返回明细 [{id, old, new}]。"""
    decay = float(cfg.get("daily_decay_factor", 0.998))
    min_w = float(cfg.get("daily_decay_min_weight", 0.1))
    reinforced = set()
    if cfg.get("daily_reinforcement_enabled"):
        meta_path = os.path.join(mgr._storage_root, "memory_meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                by_date = meta.get("used_fact_ids_by_date") or {}
                reinforced = set(by_date.get(yesterday) or [])
            except Exception:
                pass
    changes: List[Dict[str, Any]] = []
    for f in mgr.facts.get_all():
        if f.pinned or f.id in reinforced:
            continue
        new_w = round(max(min_w, f.weight * decay), 2)
        if new_w != f.weight:
            old_w = f.weight
            mgr.facts.update(f.id, weight=new_w)
            changes.append({"id": f.id, "old": old_w, "new": new_w})
    if changes:
        logger.info("[daily_memory_job] 权重衰减 profile=%s 条数=%d", mgr._profile_id, len(changes))
    return changes


def _run_reinforcement(mgr, cfg: Dict[str, Any], yesterday: str) -> List[Dict[str, Any]]:
    """对昨日被注入的 fact 略升 weight。返回明细 [{id, old, new}]。"""
    if not cfg.get("daily_reinforcement_enabled"):
        return []
    meta_path = os.path.join(mgr._storage_root, "memory_meta.json")
    if not os.path.exists(meta_path):
        return []
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        by_date = meta.get("used_fact_ids_by_date") or {}
        ids = by_date.get(yesterday) or []
    except Exception:
        return []
    changes: List[Dict[str, Any]] = []
    for fid in ids:
        fact = mgr.facts.get_by_id(fid)
        if not fact:
            continue
        new_w = round(min(2.0, fact.weight * 1.01), 2)
        if new_w > fact.weight:
            old_w = fact.weight
            mgr.facts.update(fid, weight=new_w)
            changes.append({"id": fid, "old": old_w, "new": new_w})
    if changes:
        logger.info("[daily_memory_job] 强化 profile=%s 条数=%d", mgr._profile_id, len(changes))
    return changes


def _run_orphan_cleanup(mgr) -> int:
    """删除向量库中 fact_id 不在当前 fact 列表中的向量。"""
    if not mgr.vectors or not mgr.vectors.is_available():
        return 0
    valid_ids = {f.id for f in mgr.facts.get_all()}
    n = mgr.vectors.delete_orphans(valid_ids)
    if n:
        from src.utils.debug_logger import log_vector_delete
        log_vector_delete(mgr._profile_id, n, "orphan_cleanup")
        logger.info("[daily_memory_job] 孤儿向量清理 profile=%s 删除=%d", mgr._profile_id, n)
    return n


def _select_consolidation_batch(mgr, cfg: Dict[str, Any]) -> tuple:
    """选出可合并的一批 fact：updated_at 早于 N 天、weight 低于阈值、非 pinned，最多 batch_max 条。
    返回 (batch_list, candidates_count, total_facts)。"""
    if not cfg.get("daily_consolidation_enabled"):
        return [], 0, 0
    after_days = int(cfg.get("daily_consolidation_after_days", 30))
    weight_below = float(cfg.get("daily_consolidation_weight_below", 0.3))
    batch_max = int(cfg.get("daily_consolidation_batch_max", 15))
    cutoff = (date.today() - timedelta(days=after_days)).strftime("%Y-%m-%d")
    cutoff_ts = datetime.strptime(cutoff + " 00:00:00", "%Y-%m-%d %H:%M:%S").timestamp()
    all_facts = mgr.facts.get_all()
    total_facts = len(all_facts)
    candidates = [
        f for f in all_facts
        if not f.pinned and f.updated_at < cutoff_ts and f.weight < weight_below
    ]
    candidates.sort(key=lambda x: (x.weight, x.updated_at))
    batch = candidates[:batch_max]
    return batch, len(candidates), total_facts


async def _run_consolidation(mgr, cfg: Dict[str, Any], app, profile: Dict[str, Any]) -> Dict[str, Any]:
    """执行一批合并为摘要：选批、LLM、写新摘要 + 移除旧事实向量。
    返回 detail {added, added_preview, vectors_removed, count}；无合并时返回 {}。"""
    from src.utils.debug_logger import log_memory_consolidation_skip, log_memory_forgetting_step

    batch, candidates_count, total_facts = _select_consolidation_batch(mgr, cfg)
    after_days = int(cfg.get("daily_consolidation_after_days", 30))
    weight_below = float(cfg.get("daily_consolidation_weight_below", 0.3))
    if not batch:
        logger.info(
            "[daily_memory_job] 合并为摘要：无可合并事实 profile=%s（需：超过 %d 天未更新 且 weight < %.2f 且 未钉选）",
            mgr._profile_id, after_days, weight_below,
        )
        log_memory_consolidation_skip(mgr._profile_id, after_days, weight_below, total_facts, candidates_count)
        return {}
    try:
        log_memory_forgetting_step(mgr._profile_id, "consolidation", batch_size=len(batch), candidates_count=candidates_count)
        success, detail = await mgr.run_consolidation_batch(app, profile, batch)
        return detail if success else {}
    except Exception as e:
        logger.warning("[daily_memory_job] 合并失败 profile=%s: %s", mgr._profile_id, e)
        return {}


def _any_forgetting_enabled(cfg: Dict[str, Any]) -> bool:
    """三个开关任一开启即视为「参与每日遗忘流程」。"""
    return (
        cfg.get("daily_forgetting_enabled")
        or cfg.get("daily_reinforcement_enabled")
        or cfg.get("daily_consolidation_enabled")
    )


def preview_forgetting_for_profile(
    profile_id: str, app, yesterday: Optional[str] = None
) -> Dict[str, Any]:
    """预览一次「执行一次每日记忆/遗忘任务」将执行的内容，不实际写入。返回结构化预览数据。"""
    if not yesterday:
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    out: Dict[str, Any] = {
        "profile_id": profile_id,
        "yesterday": yesterday,
        "skip": False,
        "day_summary": {"enabled": False, "would_run": False, "reason": ""},
        "decay": {"enabled": False, "would_affect_count": 0},
        "orphan_cleanup": {"enabled": False, "would_delete_count": 0},
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
    sm = getattr(app.state, "session_manager", None)
    if not sm:
        out["skip"] = True
        out["reason"] = "SessionManager 不可用"
        return out
    session = None
    for s in sm.list_sessions():
        if s.profile_id == profile_id:
            session = s
            break
    if not session:
        out["skip"] = True
        out["reason"] = "人格未加载"
        return out

    # portrait_refresh 预览（完全独立于 memory 总开关，先填好，即便后续 skip 也保留）
    try:
        from src.config.profile_loader import ProfileLoader
        _card_for_portrait = ProfileLoader().load(profile_id)
    except Exception:
        _card_for_portrait = {}
    _portrait_cfg = _card_for_portrait.get("user_portrait_config") or {}
    _portrait_block = _card_for_portrait.get("user_portrait") or {}
    _portrait_enabled = bool(_portrait_cfg.get("enabled", True))
    _portrait_auto = bool(_portrait_cfg.get("auto_refresh_in_daily_job", True))
    _portrait_min_h = float(_portrait_cfg.get("min_hours_between_refresh", 6))
    _portrait_updated = int(_portrait_block.get("updated_at") or 0)
    out["portrait_refresh"]["enabled"] = _portrait_enabled
    out["portrait_refresh"]["last_updated_at"] = _portrait_updated
    out["portrait_refresh"]["refresh_count"] = int(_portrait_block.get("refresh_count") or 0)
    if not _portrait_enabled:
        out["portrait_refresh"]["reason"] = "画像功能未启用"
    elif not _portrait_auto:
        out["portrait_refresh"]["reason"] = "已关闭日批自动刷新"
    elif _portrait_min_h > 0 and _portrait_updated > 0 and \
            (datetime.now().timestamp() - _portrait_updated) / 3600 < _portrait_min_h:
        out["portrait_refresh"]["reason"] = f"仍在冷却期内（< {_portrait_min_h:g}h）"
    else:
        out["portrait_refresh"]["would_run"] = True
        out["portrait_refresh"]["reason"] = "将尝试刷新画像"

    mgr = _get_manager_for_session(session, app)
    if not mgr:
        out["skip"] = True
        out["reason"] = "无法创建 MemoryManager"
        return out
    cfg = mgr._cfg
    if not cfg.get("enabled"):
        out["skip"] = True
        out["reason"] = "记忆未启用"
        return out

    # 日摘要预览
    day_enabled = bool(cfg.get("day_summary_enabled"))
    out["day_summary"]["enabled"] = day_enabled
    if day_enabled:
        has_summary = mgr.day_store.has_summary_for(yesterday)
        if has_summary:
            out["day_summary"]["would_run"] = False
            out["day_summary"]["reason"] = "昨日已有摘要"
        else:
            try:
                from src.config.profile_loader import ProfileLoader
                profile = ProfileLoader().load(profile_id)
            except Exception:
                profile = {}
            mem = (profile or {}).get("memory_config") or {}
            merge_enabled = mem.get("group_chat_merge_into_history") is not False and getattr(
                getattr(app, "state", None), "group_manager", None
            )
            if merge_enabled and profile:
                from src.memory.merged_history import get_merged_messages_for_date
                yesterday_msgs = get_merged_messages_for_date(session, app, profile, yesterday)
                has_conv = bool(yesterday_msgs)
            else:
                has_conv = _has_conversation_on_date(session.conversation_store, yesterday)
            if not has_conv:
                out["day_summary"]["would_run"] = True
                out["day_summary"]["reason"] = "昨日无对话（将生成安静日记）"
            else:
                out["day_summary"]["would_run"] = True
                out["day_summary"]["reason"] = "将生成昨日摘要"

    # 衰减：仅统计会被衰减的 fact 数（不写入）
    decay_enabled = bool(cfg.get("daily_forgetting_enabled"))
    out["decay"]["enabled"] = decay_enabled
    if decay_enabled:
        reinforced = set()
        if cfg.get("daily_reinforcement_enabled"):
            meta_path = os.path.join(mgr._storage_root, "memory_meta.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    by_date = meta.get("used_fact_ids_by_date") or {}
                    reinforced = set(by_date.get(yesterday) or [])
                except Exception:
                    pass
        decay = float(cfg.get("daily_decay_factor", 0.998))
        min_w = float(cfg.get("daily_decay_min_weight", 0.1))
        count = 0
        for f in mgr.facts.get_all():
            if f.pinned or f.id in reinforced:
                continue
            new_w = round(max(min_w, f.weight * decay), 2)
            if new_w != f.weight:
                count += 1
        out["decay"]["would_affect_count"] = count

    # 孤儿向量：仅统计数量（不删除）
    out["orphan_cleanup"]["enabled"] = decay_enabled and bool(mgr.vectors and mgr.vectors.is_available())
    if out["orphan_cleanup"]["enabled"]:
        valid_ids = {f.id for f in mgr.facts.get_all()}
        out["orphan_cleanup"]["would_delete_count"] = mgr.vectors.count_orphans(valid_ids)

    # 强化：统计会被强化的 fact 数
    rein_enabled = bool(cfg.get("daily_reinforcement_enabled"))
    out["reinforcement"]["enabled"] = rein_enabled
    if rein_enabled:
        meta_path = os.path.join(mgr._storage_root, "memory_meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                by_date = meta.get("used_fact_ids_by_date") or {}
                ids = by_date.get(yesterday) or []
                count = 0
                for fid in ids:
                    fact = mgr.facts.get_by_id(fid)
                    if fact and fact.weight < 2.0:
                        count += 1
                out["reinforcement"]["would_affect_count"] = count
            except Exception:
                pass

    # 合并：选批结果与预览
    cons_enabled = bool(cfg.get("daily_consolidation_enabled"))
    out["consolidation"]["enabled"] = cons_enabled
    if cons_enabled:
        batch, candidates_count, total_facts = _select_consolidation_batch(mgr, cfg)
        out["consolidation"]["total_facts"] = total_facts
        out["consolidation"]["candidates_count"] = candidates_count
        out["consolidation"]["batch_size"] = len(batch)
        out["consolidation"]["would_run"] = len(batch) > 0
        out["consolidation"]["preview_facts"] = [f.content[:60] + ("…" if len(f.content) > 60 else "") for f in batch[:5]]
    return out


async def run_forgetting_for_profile(profile_id: str, app, yesterday: Optional[str] = None, *, force: bool = False) -> None:
    """对指定人格执行遗忘。三个开关独立：
    - 启用每日遗忘：执行权重衰减 + 孤儿向量清理
    - 强化：昨日被用过的 fact 略升 weight
    - 合并为摘要：将淡事实合并为 1～2 条
    定时任务仅当至少一个开关开启时才进入；手动触发(force=True)时同样按各自开关执行。"""
    if not yesterday:
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    if force:
        logger.info("[daily_memory_job] 手动执行遗忘任务 profile=%s", profile_id)
    sm = getattr(app.state, "session_manager", None)
    if not sm:
        if force:
            logger.warning("[daily_memory_job] SessionManager 不可用，跳过")
        return
    session = None
    for s in sm.list_sessions():
        if s.profile_id == profile_id:
            session = s
            break
    if not session:
        logger.warning("[daily_memory_job] profile %s 未加载，跳过遗忘", profile_id)
        if force:
            from src.utils.debug_logger import log_memory_forgetting_skip
            log_memory_forgetting_skip(profile_id, "profile_not_loaded")
        return

    mgr = _get_manager_for_session(session, app)
    if not mgr:
        if force:
            logger.warning("[daily_memory_job] 无法创建 MemoryManager profile=%s", profile_id)
            from src.utils.debug_logger import log_memory_forgetting_skip
            log_memory_forgetting_skip(profile_id, "memory_manager_create_failed")
        return
    cfg = mgr._cfg
    if not cfg.get("enabled"):
        if force:
            logger.info("[daily_memory_job] profile %s 记忆未启用，跳过", profile_id)
            from src.utils.debug_logger import log_memory_forgetting_skip
            log_memory_forgetting_skip(profile_id, "memory_disabled")
        return
    # 定时任务：三个开关都关则不跑；手动触发仍按各自开关执行
    if not force and not _any_forgetting_enabled(cfg):
        logger.debug("[daily_memory_job] profile %s 未开启任一遗忘/强化/合并，跳过", profile_id)
        return

    from src.utils.debug_logger import log_memory_forgetting_run, log_memory_forgetting_step

    steps_enabled = {
        "daily_forgetting_enabled": bool(cfg.get("daily_forgetting_enabled")),
        "daily_reinforcement_enabled": bool(cfg.get("daily_reinforcement_enabled")),
        "daily_consolidation_enabled": bool(cfg.get("daily_consolidation_enabled")),
    }
    log_memory_forgetting_run(profile_id, "manual" if force else "scheduled", steps_enabled)

    decay_changes: List[Dict[str, Any]] = []
    reinforce_changes: List[Dict[str, Any]] = []
    consolidation_detail: Dict[str, Any] = {}
    orphan_count = 0
    if cfg.get("daily_forgetting_enabled"):
        decay_changes = await asyncio.to_thread(_run_weight_decay, mgr, cfg, yesterday)
        log_memory_forgetting_step(profile_id, "decay", decayed_count=len(decay_changes))
        orphan_count = await asyncio.to_thread(_run_orphan_cleanup, mgr)
        log_memory_forgetting_step(profile_id, "orphan_cleanup", deleted=orphan_count)
    if cfg.get("daily_reinforcement_enabled"):
        reinforce_changes = await asyncio.to_thread(_run_reinforcement, mgr, cfg, yesterday)
        log_memory_forgetting_step(profile_id, "reinforcement", count=len(reinforce_changes))
    if cfg.get("daily_consolidation_enabled"):
        from src.config.profile_loader import ProfileLoader
        try:
            profile = ProfileLoader().load(profile_id)
        except Exception:
            profile = {}
        consolidation_detail = await _run_consolidation(mgr, cfg, app, profile)
        log_memory_forgetting_step(profile_id, "consolidation_done", added=consolidation_detail.get("count", 0))

    # 记忆变更日志：实际改动了事实才记（空跑不记）
    try:
        _record_memory_changelog(session.storage_root, yesterday, force, mgr, cfg,
                                 decay_changes, reinforce_changes, consolidation_detail, orphan_count)
    except Exception as e:
        logger.warning("[daily_memory_job] 记忆变更日志写入失败（非致命）: %s", e)

    if force:
        logger.info("[daily_memory_job] 手动执行遗忘完成 profile=%s", profile_id)


def _record_memory_changelog(storage_root: str, date_str: str, force: bool, mgr, cfg: Dict[str, Any],
                             decay: List[Dict], reinforce: List[Dict],
                             cons: Dict[str, Any], orphan: int) -> None:
    """把一次遗忘任务的明细组装成 changelog 条目；仅在实际改动了事实时写入。"""
    from src.memory import memory_changelog

    rollback_data = {
        "decay": decay,
        "reinforce": reinforce,
        "consolidation": {
            "added": cons.get("added", []),
            "vectors_removed": cons.get("vectors_removed", []),
        },
    }
    if not memory_changelog.has_meaningful_changes(rollback_data):
        return

    min_w = float(cfg.get("daily_decay_min_weight", 0.1))
    decay_floored: List[str] = []
    for c in decay:
        if c.get("new", 1.0) <= min_w:
            fact = mgr.facts.get_by_id(c.get("id"))
            if fact:
                decay_floored.append(fact.content[:60])

    entry = {
        "run_id": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "timestamp": int(time.time()),
        "trigger": "manual" if force else "auto",
        "date": date_str,
        "rolled_back": False,
        "summary": {
            "decay_count": len(decay),
            "decay_floored": decay_floored[:5],
            "reinforce_count": len(reinforce),
            "consolidation": {
                "added_count": len(cons.get("added", [])),
                "vectors_removed_count": len(cons.get("vectors_removed", [])),
                "added_preview": cons.get("added_preview", []),
            },
            "orphan_vectors": orphan,
        },
        "rollback_data": rollback_data,
    }
    memory_changelog.record_run(storage_root, entry)


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


async def run_daily_for_profile(profile_id: str, yesterday: str, app) -> None:
    """对一个人格跑完整每日任务：先日摘要再遗忘，最后画像刷新。无对话时仍写安静日记，但跳过遗忘任务。"""
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

    await run_day_summary_for_profile(profile_id, yesterday, app)

    store = session.conversation_store
    if _has_conversation_on_date(store, yesterday):
        await run_forgetting_for_profile(profile_id, app, yesterday)

    # portrait 完全独立于 memory 总开关，单独检查自己的 enabled / auto_refresh_in_daily_job
    await run_portrait_refresh_for_profile(profile_id, app)

    _save_last_daily_run(session.storage_root, date.today().strftime("%Y-%m-%d"))


async def run_daily_memory_loop(app) -> None:
    """每日任务主循环：sleep 到下一日 00:05（或可配置时刻），遍历已加载且昨日有对话的 session 执行任务。"""
    logger.info("[daily_memory_job] 每日记忆/遗忘循环已启动")
    while True:
        try:
            today = date.today()
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
