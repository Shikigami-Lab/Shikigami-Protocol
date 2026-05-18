"""单聊与群聊记录合并：近期消息与按日消息的统一接口。

供 HistorySegment、Reflection、Emotion、Affinity、Memory（auto_extract、day_summary）、
日摘要预览等使用。当 memory_config.group_chat_merge_into_history 为 False 时回退为仅单聊 store。

get_merged_user_activity：按人格聚合单聊+所有参与群的用户消息统计，供 time_context 与频率面板等使用。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.memory.conversation_store import ConversationStore

logger = logging.getLogger(__name__)

_DEFAULT_MAX_UTTERANCES_PER_GROUP = 10
_DEFAULT_MAX_GROUPS = 5


def _msg_date(msg) -> str:
    """从消息的 timestamp 提取 YYYY-MM-DD。支持 dict 和带 timestamp 的对象。"""
    ts = msg.get("timestamp") if isinstance(msg, dict) else getattr(msg, "timestamp", None)
    if ts:
        try:
            # 本地时区：与 chat / daily_memory_job / memory_manager 的 _msg_date 一致，
            # 否则同一条消息在不同读取路径会落到不同日期，日摘要会取错某天的对话。
            return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
        except Exception:
            pass
    return ""


def _merge_enabled_and_caps(profile: Dict[str, Any]) -> tuple:
    """(merge_enabled, max_utterances_per_group, max_groups, merged_cap). merged_cap 由调用方 n_turns 算时再乘 2 或读配置。"""
    mem = (profile.get("memory_config") or {})
    merge_enabled = mem.get("group_chat_merge_into_history")
    if merge_enabled is False:
        return False, 10, 5, 0

    max_utterances = mem.get("group_chat_self_recap_max_utterances_per_group")
    if max_utterances is None:
        max_utterances = _DEFAULT_MAX_UTTERANCES_PER_GROUP
    else:
        try:
            max_utterances = max(1, min(100, int(max_utterances)))
        except (TypeError, ValueError):
            max_utterances = _DEFAULT_MAX_UTTERANCES_PER_GROUP

    max_groups = mem.get("group_chat_self_recap_max_groups")
    if max_groups is None:
        max_groups = _DEFAULT_MAX_GROUPS
    else:
        try:
            max_groups = max(1, min(20, int(max_groups)))
        except (TypeError, ValueError):
            max_groups = _DEFAULT_MAX_GROUPS

    merged_cap = mem.get("merged_history_max_messages")
    if merged_cap is None:
        merged_cap = 0  # 表示由调用方用 n_turns*2
    else:
        try:
            merged_cap = max(2, min(500, int(merged_cap)))
        except (TypeError, ValueError):
            merged_cap = 0
    return True, max_utterances, max_groups, merged_cap


def get_merged_recent_messages(
    session: Any,
    app: Any,
    profile: Dict[str, Any],
    n_turns: int,
    cap: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """近期消息：单聊 + 该人格参与的群聊在 context 时间窗内合并，按时间排序并 cap。

    当 group_chat_merge_into_history 为 False 时，仅返回单聊 store 的 get_recent_with_ts，
    并统一为与合并时相同的 dict 结构（含 role, content, sender, timestamp, source='single'）。

    返回每项: role, content, sender, timestamp, source('single'|'group'), gname(仅 group)。
    """
    store = session.conversation_store
    merge_enabled, max_utterances, max_groups, merged_cap_cfg = _merge_enabled_and_caps(profile)
    merged_cap = cap if cap is not None else (merged_cap_cfg if merged_cap_cfg > 0 else n_turns * 2)
    merged_cap = max(2, merged_cap)

    if not merge_enabled:
        raw = store.get_recent_with_ts(n_turns)
        return [
            {
                "role": m.get("role") or "user",
                "content": (m.get("content") or "").strip(),
                "sender": (m.get("sender") or "").strip(),
                "timestamp": float(m.get("timestamp") or 0),
                "source": "single",
            }
            for m in raw
        ]

    try:
        context_start_ts = store.get_context_start_ts(n_turns)
    except Exception:
        context_start_ts = 0.0

    combined: List[Dict[str, Any]] = []
    single_msgs = store.get_recent_with_ts(n_turns)
    for m in single_msgs:
        combined.append({
            "ts": float(m.get("timestamp") or 0),
            "role": m.get("role") or "user",
            "content": (m.get("content") or "").strip(),
            "source": "single",
            "sender": (m.get("sender") or "").strip(),
        })

    group_manager = getattr(getattr(app, "state", None), "group_manager", None) if app else None
    if group_manager:
        profile_id = getattr(session, "profile_id", None) or (profile.get("id") if isinstance(profile.get("id"), str) else None)
        if profile_id:
            group_ids = group_manager.get_group_ids_for_profile(profile_id)
            for gid in group_ids[:max_groups]:
                cfg = group_manager.get_group(gid)
                gname = (cfg.get("display_name") or gid).strip() or gid if cfg else gid
                gstore = group_manager.get_conversation_store(gid)
                if not gstore:
                    continue
                # 尊重该群自己的「短期重置」：只取该群 context_since_ts 之后的消息，否则重置后仍会看到旧群聊
                group_since = getattr(gstore, "context_since_ts", 0.0) or 0.0
                since_ts = max(context_start_ts, group_since)
                try:
                    group_msgs = gstore.get_recent_since_with_ts(since_ts, max_utterances)
                except Exception:
                    continue
                for m in group_msgs:
                    role = m.get("role") or "user"
                    content = (m.get("content") or "").strip()
                    if not content:
                        continue
                    sender = (m.get("sender") or "").strip() or ("用户" if role == "user" else "助手")
                    combined.append({
                        "ts": float(m.get("timestamp") or 0),
                        "role": role,
                        "content": content,
                        "source": "group",
                        "gname": gname,
                        "sender": sender,
                    })

    combined.sort(key=lambda x: x["ts"])
    capped = combined[-merged_cap:] if len(combined) > merged_cap else combined

    out: List[Dict[str, Any]] = []
    for m in capped:
        out.append({
            "role": m["role"],
            "content": m["content"],
            "sender": m.get("sender", ""),
            "timestamp": m["ts"],
            "source": m.get("source", "single"),
            "gname": m.get("gname", ""),
        })
    return out


def get_merged_messages_for_date(
    session: Any,
    app: Any,
    profile: Dict[str, Any],
    date_str: str,
) -> List[Dict[str, Any]]:
    """某日全天消息：单聊 + 该人格参与的群聊当日消息合并，按时间排序。

    当 group_chat_merge_into_history 为 False 时，仅返回单聊 store 该日消息（dict 列表，含 role, content, timestamp, sender），
    与 memory_manager 的 _msg_date / _msg_role 兼容。
    """
    store = session.conversation_store
    merge_enabled, _max_utterances, max_groups, _ = _merge_enabled_and_caps(profile)

    def to_dict(m: Any) -> Dict[str, Any]:
        if isinstance(m, dict):
            return {
                "role": m.get("role", "user"),
                "content": (m.get("content") or "").strip(),
                "timestamp": float(m.get("timestamp") or 0),
                "sender": (m.get("sender") or "").strip(),
            }
        return {
            "role": getattr(m, "role", "user"),
            "content": (getattr(m, "content", None) or "").strip(),
            "timestamp": float(getattr(m, "timestamp", 0)),
            "sender": (getattr(m, "sender", None) or "").strip(),
        }

    day_list: List[Dict[str, Any]] = []
    try:
        all_single = store.get_all()
    except Exception:
        all_single = []
    for m in all_single:
        d = to_dict(m)
        if _msg_date(d) == date_str and d.get("role") in ("user", "assistant"):
            d["source"] = "single"
            day_list.append(d)
    # 若未启用合并，直接返回单聊当日
    if not merge_enabled:
        day_list.sort(key=lambda x: x["timestamp"])
        return day_list

    group_manager = getattr(getattr(app, "state", None), "group_manager", None) if app else None
    if not group_manager:
        day_list.sort(key=lambda x: x["timestamp"])
        return day_list

    profile_id = getattr(session, "profile_id", None) or (profile.get("id") if isinstance(profile.get("id"), str) else None)
    if not profile_id:
        day_list.sort(key=lambda x: x["timestamp"])
        return day_list

    for gid in group_manager.get_group_ids_for_profile(profile_id)[:max_groups]:
        cfg = group_manager.get_group(gid)
        gname = (cfg.get("display_name") or gid).strip() or gid if cfg else gid
        gstore = group_manager.get_conversation_store(gid)
        if not gstore:
            continue
        try:
            all_group = gstore.get_all()
        except Exception:
            continue
        for m in all_group:
            d = to_dict(m)
            if _msg_date(d) != date_str or d.get("role") not in ("user", "assistant"):
                continue
            d["source"] = "group"
            d["gname"] = gname
            day_list.append(d)

    day_list.sort(key=lambda x: x["timestamp"])
    return day_list


def get_merged_user_activity(
    profile_id: str,
    app: Any,
    user_name: str = "用户",
) -> Dict[str, Any]:
    """按人格聚合「单聊 + 该人格参与的所有群」的用户消息统计，供 time_context 与频率面板使用。

    返回：last_user_ts (float|None), total_count, count_today, count_24h, count_7d。
    """
    if not (profile_id and str(profile_id).strip()):
        return {
            "last_user_ts": None,
            "total_count": 0,
            "count_today": 0,
            "count_24h": 0,
            "count_7d": 0,
        }
    pid = str(profile_id).strip()
    user_name = (user_name or "用户").strip() or "用户"

    single_root = f"profiles/{pid}"
    try:
        single_store = ConversationStore(single_root, max_turns=0)
    except Exception as e:
        logger.warning("[get_merged_user_activity] single store failed profile=%s: %s", pid, e)
        return {
            "last_user_ts": None,
            "total_count": 0,
            "count_today": 0,
            "count_24h": 0,
            "count_7d": 0,
        }

    now = time.time()
    today_start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    since_24h = now - 86400
    since_7d = now - 7 * 86400

    last_user_ts = single_store.last_user_message_time(user_name)
    total_count = single_store.total_user_messages_count(user_name)
    count_today = single_store.count_user_messages_since(today_start, user_name)
    count_24h = single_store.count_user_messages_since(since_24h, user_name)
    count_7d = single_store.count_user_messages_since(since_7d, user_name)

    group_manager = getattr(getattr(app, "state", None), "group_manager", None) if app else None
    if group_manager:
        for gid in group_manager.get_group_ids_for_profile(pid):
            gstore = group_manager.get_conversation_store(gid)
            if not gstore:
                continue
            try:
                ts = gstore.last_user_message_time(user_name)
                if ts is not None and (last_user_ts is None or ts > last_user_ts):
                    last_user_ts = ts
                total_count += gstore.total_user_messages_count(user_name)
                count_today += gstore.count_user_messages_since(today_start, user_name)
                count_24h += gstore.count_user_messages_since(since_24h, user_name)
                count_7d += gstore.count_user_messages_since(since_7d, user_name)
            except Exception as e:
                logger.debug("[get_merged_user_activity] group %s: %s", gid, e)

    return {
        "last_user_ts": last_user_ts,
        "total_count": total_count,
        "count_today": count_today,
        "count_24h": count_24h,
        "count_7d": count_7d,
    }
