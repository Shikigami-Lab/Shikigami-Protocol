"""
debug_logger.py — Shikigami Protocol structured event logger

Events are written as JSONL (one JSON object per line) into separate log files
so each concern can be inspected independently.

Log files (all in data/logs/)
──────────────────────────────
  llm_calls.log      Full messages[] array sent to LLM + model + gen params
                     ← most important: use this to debug prompts
  llm_responses.log  Complete assistant reply after streaming finishes
  tts.log            TTS request text + synthesis result (bytes / error)
  app.log            Server start, session events, chat requests, errors
  debug.log          Timer/SSE/broadcast 诊断（计时器到期回调、SSE 连接/断开、push_one 订阅数）
  emotion.log        One JSON object per line: emotion classify (old/new layers, classifier_results, input_text, reason)
  affinity.log       One JSON object per line: affinity adjust (old/new affinity, delta, input_text, reason)
  build_timings.log     每轮对话一条：build_messages 各 segment 用时、总 build 时间、LLM 流式用时（debug 性能）
  tools.log          工具输入/输出详情：Timer（开始/停止/到期/查询）、Todo（增删改查）、WebSearch（请求+结果+引擎）

Rotation: each file is capped at 10 MB; up to 3 backups are kept (.log.1 … .log.3).
This module never raises — all writes are best-effort.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

_LOG_DIR = "data/logs"

_LLM_CALLS_LOG   = f"{_LOG_DIR}/llm_calls.log"
_LLM_REPLIES_LOG = f"{_LOG_DIR}/llm_responses.log"
_TTS_LOG         = f"{_LOG_DIR}/tts.log"
_APP_LOG         = f"{_LOG_DIR}/app.log"
_EMOTION_LOG     = f"{_LOG_DIR}/emotion.log"
_AFFINITY_LOG    = f"{_LOG_DIR}/affinity.log"
_MEMORY_LOG      = f"{_LOG_DIR}/memory.log"

_REFLECTION_LOG = f"{_LOG_DIR}/reflection.log"
_ASE_LOG        = f"{_LOG_DIR}/ase.log"
_VLM_LOG        = f"{_LOG_DIR}/vlm.log"

# Secondary-model LLM call/response logs (emotion, affinity, reflection,
# memory_extract, memory_summary, vlm).  Separate from the main-model logs
# so llm_calls.log stays focused on the user-facing chat flow.
_SECONDARY_LLM_CALLS_LOG   = f"{_LOG_DIR}/secondary_llm_calls.log"
_SECONDARY_LLM_RESPONSES_LOG = f"{_LOG_DIR}/secondary_llm_responses.log"

# Frontend UI debug events forwarded to disk via POST /debug/ui_log
_UI_DEBUG_LOG = f"{_LOG_DIR}/ui_debug.log"

# build_messages + LLM 耗时（每轮对话一条 JSONL，含各 segment 与 llm_stream_ms）
_BUILD_TIMINGS_LOG = f"{_LOG_DIR}/build_timings.log"

# 诊断用：计时器到期回调、SSE 连接/断开、broadcast push_one 等（不写 app.log）
_DEBUG_LOG = f"{_LOG_DIR}/debug.log"

# 工具详细输入/输出（Timer / Todo / WebSearch）
_TOOLS_LOG = f"{_LOG_DIR}/tools.log"

_MAX_BYTES    = 10 * 1024 * 1024  # 10 MB per file
_BACKUP_COUNT = 3


# ─────────────────────────── internals ───────────────────────────

def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def _rotate(path: str) -> None:
    """Shift .log → .log.1 → .log.2 → .log.3；只保留 _BACKUP_COUNT 个备份，不产生 .4。"""
    try:
        if not os.path.exists(path):
            return
        if os.path.getsize(path) < _MAX_BYTES:
            return
        # 先删最老的 .log.3，再从高到低移位 .2→.3, .1→.2，最后 path→.1
        oldest = f"{path}.{_BACKUP_COUNT}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(_BACKUP_COUNT - 1, 0, -1):
            src = f"{path}.{i}"
            dst = f"{path}.{i + 1}"
            if os.path.exists(src):
                os.rename(src, dst)
        os.rename(path, f"{path}.1")
    except Exception:
        pass


def _write(path: str, event: str, data: Dict[str, Any]) -> None:
    try:
        _ensure_dir(path)
        _rotate(path)
        entry = {
            "ts": time.time(),
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "event": event,
            **data,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # best-effort — never propagate


def log_debug(event: str, **data: Any) -> None:
    """写入 debug.log（JSONL），用于计时器/SSE/broadcast/LLM 空或错误等诊断，不写 app.log。"""
    _write(_DEBUG_LOG, event, data)


# ─────────────────────────── llm_calls.log ───────────────────────
# The most important debug artifact: the exact payload sent to the LLM.
# Each line contains the full messages[] list (system, history, user turn)
# plus model name and gen params.
#
# Quick filter:
#   cat data/logs/llm_calls.log | jq '.messages[] | select(.role=="system") | .content'

def log_llm_call(
    messages: List[Dict[str, str]],
    model: str,
    gen_kwargs: Dict[str, Any],
    session_id: str = "",
) -> None:
    _write(_LLM_CALLS_LOG, "llm_call", {
        "session_id": session_id,
        "model": model,
        "gen_kwargs": gen_kwargs,
        "message_count": len(messages),
        "messages": messages,
    })


# ─────────────────────────── llm_responses.log ───────────────────

def log_llm_response(session_id: str, response: str, model: str) -> None:
    _write(_LLM_REPLIES_LOG, "llm_response", {
        "session_id": session_id,
        "model": model,
        "response": response,
        "char_count": len(response),
    })


# ─────────────────────────── build_timings.log ───────────────────
# 每轮对话一条：build_messages 各 segment 用时 + 总 build 时间 + LLM 流式用时

def log_build_timings(
    session_id: str,
    build_timings: Dict[str, Any],
    llm_stream_ms: float,
    model: str = "",
) -> None:
    """写入 build_timings.log：profile_load_ms, segments{segment_id: ms}, assemble_ms, total_build_ms, llm_stream_ms."""
    _write(_BUILD_TIMINGS_LOG, "build_timings", {
        "session_id": session_id,
        "profile_load_ms": build_timings.get("profile_load_ms", 0),
        "segments": build_timings.get("segments", {}),
        "assemble_ms": build_timings.get("assemble_ms", 0),
        "total_build_ms": build_timings.get("total_build_ms", 0),
        "llm_stream_ms": round(llm_stream_ms, 2),
        "model": model,
        "total_ms": round(build_timings.get("total_build_ms", 0) + llm_stream_ms, 2),
    })


# ─────────────────────────── tts.log ─────────────────────────────

def log_tts_request(text: str, voice: str, session_id: str = "") -> None:
    _write(_TTS_LOG, "tts_request", {
        "session_id": session_id,
        "voice": voice,
        "text": text,
        "text_len": len(text),
    })


def log_tts_response(
    text: str,
    voice: str,
    audio_bytes: int,
    success: bool,
    error: str = "",
) -> None:
    _write(_TTS_LOG, "tts_response", {
        "voice": voice,
        "text_preview": text[:100],
        "audio_bytes": audio_bytes,
        "success": success,
        "error": error,
    })


# ─────────────────────────── app.log ─────────────────────────────

def log_server_start(host: str, port: int, default_llm: str, default_tts: str) -> None:
    _write(_APP_LOG, "server_start", {
        "host": host,
        "port": port,
        "default_llm": default_llm,
        "default_tts": default_tts,
    })


def log_chat_request(session_id: str, session_name: str, user_message: str) -> None:
    _write(_APP_LOG, "chat_request", {
        "session_id": session_id,
        "session_name": session_name,
        "user_message": user_message,
        "user_message_len": len(user_message),
    })


def log_session_switch(from_id: str, to_id: str, to_name: str) -> None:
    _write(_APP_LOG, "session_switch", {
        "from_session_id": from_id,
        "to_session_id": to_id,
        "to_session_name": to_name,
    })


def log_session_create(session_id: str, profile_id: str, display_name: str) -> None:
    _write(_APP_LOG, "session_create", {
        "session_id": session_id,
        "profile_id": profile_id,
        "display_name": display_name,
    })


def log_error(context: str, error: str, extra: Dict[str, Any] = None, traceback_str: Optional[str] = None) -> None:
    """写入 app.log：仅用于 error。extra 与 traceback_str 会一并写入，便于排查。"""
    payload = {"context": context, "error": error, **(extra or {})}
    if traceback_str:
        payload["traceback"] = traceback_str
    _write(_APP_LOG, "error", payload)


def log_app_event(event: str, **data: Any) -> None:
    """写入 app.log：任意事件（如 forgetting_run_request），便于排查。"""
    _write(_APP_LOG, event, data)


def log_backup(session_id: str, backup_dir: str, files: List[str], error: str = "") -> None:
    _write(_APP_LOG, "backup", {
        "session_id": session_id,
        "backup_dir": backup_dir,
        "files": files,
        "ok": not error,
        "error": error,
    })


def log_todo(session_id: str, action: str, content: str = "", todo_id: str = "") -> None:
    """action: add | complete | delete | list"""
    _write(_APP_LOG, "todo", {
        "session_id": session_id,
        "action": action,
        "todo_id": todo_id,
        "content_preview": content[:80],
    })


def log_timer(session_id: str, action: str, timer_id: str = "",
              label: str = "", seconds: int = 0, error: str = "") -> None:
    """action: start | stop | expire | list"""
    _write(_APP_LOG, "timer", {
        "session_id": session_id,
        "action": action,
        "timer_id": timer_id,
        "label": label,
        "seconds": seconds,
        "error": error,
    })


# ── P4 引擎事件日志 ───────────────────────────────────────────────────────────

def log_engine_update(engine: str, session_id: str, delta: Dict[str, Any], reason: str = "") -> None:
    """记录引擎状态变化（情绪/能量/好感度更新）。"""
    _write(_APP_LOG, "engine_update", {
        "engine": engine,
        "session_id": session_id,
        "delta": delta,
        "reason": reason,
    })


def log_classify_call(session_id: str, result: Dict[str, Any], model: str) -> None:
    """记录情绪分类器 LLM 调用结果（同时写入 app.log）。"""
    _write(_APP_LOG, "emotion_classify", {
        "session_id": session_id,
        "result": result,
        "model": model,
    })


# ── emotion.log（每行一个完整情绪分类条目，便于排查与复现）────────────────────────

def log_emotion_entry(entry: Dict[str, Any]) -> None:
    """写入 emotion.log：一行一个 JSON 对象。entry 需包含 timestamp/turn_count/old_emotions/classifier_results/new_emotions/input_text/result_emotion 等。"""
    try:
        _ensure_dir(_EMOTION_LOG)
        _rotate(_EMOTION_LOG)
        if "timestamp" not in entry:
            entry = {"timestamp": time.time(), **entry}
        with open(_EMOTION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── affinity.log（每行一个好感度调整条目）──────────────────────────────────────

# ── memory.log（记忆模块结构化事件）──────────────────────────────────────────────

def log_memory_startup(profile_id: str, facts_count: int, vector_ok: bool,
                       vector_count: int, vector_provider: str,
                       summary_count: int) -> None:
    """记录 MemoryManager 启动/初始化状态。"""
    _write(_MEMORY_LOG, "memory_startup", {
        "profile_id": profile_id,
        "facts_count": facts_count,
        "vector_ok": vector_ok,
        "vector_count": vector_count,
        "vector_provider": vector_provider,
        "summary_count": summary_count,
    })


def log_fact_add(
    profile_id: str,
    fact_id: str,
    content: str,
    category: str,
    source: str,
    *,
    weight: float = 1.0,
    tags: Optional[List[str]] = None,
    pinned: bool = False,
    emotional_note: str = "",
    updated_at: Optional[float] = None,
    is_manual: bool = False,
) -> None:
    """记录一条事实被添加到 LongTermStore。事件名 fact_add，与 vector_add / fact_delete / vector_delete 对称。"""
    payload: Dict[str, Any] = {
        "profile_id": profile_id,
        "fact_id": fact_id,
        "category": category,
        "source": source,
        "content_preview": content[:120],
        "weight": weight,
        "tags": tags if tags is not None else [],
        "pinned": pinned,
        "emotional_note": emotional_note,
        "is_manual": is_manual,
    }
    if updated_at is not None:
        payload["updated_at"] = updated_at
    _write(_MEMORY_LOG, "fact_add", payload)


def log_fact_delete(
    profile_id: str,
    fact_id: str,
    content_preview: str,
    source: str,
) -> None:
    """记录一条事实被删除。source: consolidation | api | command。"""
    _write(_MEMORY_LOG, "fact_delete", {
        "profile_id": profile_id,
        "fact_id": fact_id,
        "content_preview": (content_preview or "")[:120],
        "source": source,
    })


def log_fact_clear_all(profile_id: str, deleted_count: int, source: str = "api") -> None:
    """记录清空全部事实。"""
    _write(_MEMORY_LOG, "fact_clear_all", {
        "profile_id": profile_id,
        "deleted_count": deleted_count,
        "source": source,
    })


def log_vector_delete(
    profile_id: str,
    deleted_count: int,
    reason: str,
    fact_ids: Optional[List[str]] = None,
) -> None:
    """记录向量被删除。reason: orphan_cleanup | consolidation | api_clear_all。"""
    payload: Dict[str, Any] = {
        "profile_id": profile_id,
        "deleted_count": deleted_count,
        "reason": reason,
    }
    if fact_ids:
        payload["fact_ids"] = fact_ids[:20]
    _write(_MEMORY_LOG, "vector_delete", payload)


def log_vector_add(profile_id: str, content: str, fact_id: str = "",
                   replaced: bool = False) -> None:
    """记录一条向量被写入 VectorMemoryStore。"""
    _write(_MEMORY_LOG, "vector_add", {
        "profile_id": profile_id,
        "fact_id": fact_id,
        "replaced": replaced,
        "content_preview": content[:120],
    })


def log_memory_extract(profile_id: str, turn: int, extracted: int,
                       added: int) -> None:
    """记录一次 auto_extract LLM 调用结果。"""
    _write(_MEMORY_LOG, "memory_extract", {
        "profile_id": profile_id,
        "turn": turn,
        "extracted_count": extracted,
        "added_count": added,
    })


def log_summary_generate(profile_id: str, date_str: str,
                          msg_count: int, summary_preview: str) -> None:
    """记录一次日摘要生成。"""
    _write(_MEMORY_LOG, "summary_generate", {
        "profile_id": profile_id,
        "date": date_str,
        "msg_count": msg_count,
        "summary_preview": summary_preview[:200],
    })


def log_auto_extract_emotion_context(profile_id: str, has_context: bool, primary_emotion: str = "") -> None:
    """记录 auto_extract 调用时是否带上 emotion_context（用于确认向量 metadata 可含 ai_emotion）。"""
    _write(_MEMORY_LOG, "auto_extract_emotion_context", {
        "profile_id": profile_id,
        "has_emotion_context": has_context,
        "primary_emotion": primary_emotion[:32] if primary_emotion else "",
    })


def log_memory_forgetting_run(
    profile_id: str,
    trigger: str,
    steps_enabled: Dict[str, bool],
) -> None:
    """记录一次每日记忆/遗忘任务开始。trigger=manual|scheduled。"""
    _write(_MEMORY_LOG, "forgetting_run", {
        "profile_id": profile_id,
        "trigger": trigger,
        "steps_enabled": steps_enabled,
    })


def log_memory_forgetting_step(
    profile_id: str,
    step: str,
    **detail: Any,
) -> None:
    """记录遗忘某一步执行结果。step=decay|orphan_cleanup|reinforcement|consolidation。"""
    _write(_MEMORY_LOG, "forgetting_step", {
        "profile_id": profile_id,
        "step": step,
        **detail,
    })


def log_memory_consolidation_skip(
    profile_id: str,
    after_days: int,
    weight_below: float,
    total_facts: int,
    candidates_count: int,
) -> None:
    """记录合并为摘要因无可合并事实而跳过（便于 debug 为何无 LLM 调用）。"""
    _write(_MEMORY_LOG, "consolidation_skip", {
        "profile_id": profile_id,
        "after_days": after_days,
        "weight_below": weight_below,
        "total_facts": total_facts,
        "candidates_count": candidates_count,
        "reason": "no facts meet: not pinned, updated_at older than after_days, weight < weight_below",
    })


def log_memory_forgetting_skip(profile_id: str, reason: str) -> None:
    """记录遗忘任务未执行（如未加载、记忆未启用、未开任一开关），便于 debug。"""
    _write(_MEMORY_LOG, "forgetting_skip", {
        "profile_id": profile_id,
        "reason": reason,
    })


# ── reflection.log（自省引擎结构化事件）────────────────────────────────────────

def log_reflection_result(
    session_id: str,
    profile_id: str,
    thought: str,
    style_hint: str,
    urgency: float,
    topic_anchor: str,
    model: str,
    used_fallback: bool,
    recent_turns_used: int,
    duration_ms: int,
) -> None:
    """记录一次成功的自省 LLM 调用结果。"""
    _write(_REFLECTION_LOG, "reflection_result", {
        "session_id": session_id,
        "profile_id": profile_id,
        "thought": thought[:200],
        "style_hint": style_hint[:200],
        "urgency": urgency,
        "topic_anchor": topic_anchor[:100],
        "model": model,
        "used_fallback": used_fallback,
        "recent_turns_used": recent_turns_used,
        "duration_ms": duration_ms,
    })


def log_reflection_skip(session_id: str, reason: str, error: str = "") -> None:
    """记录自省被跳过的情况（无近期对话、模型不可用等）。
    reason: no_recent_turns | model_disabled | exception"""
    _write(_REFLECTION_LOG, "reflection_skip", {
        "session_id": session_id,
        "reason": reason,
        "error": error,
    })


# ── ase.log（主动发言决策结构化事件）──────────────────────────────────────────

def log_ase_check(
    session_id: str,
    mode: str,
    urgency: float,
    threshold: float,
    silent_seconds: float,
    decision: str,
    skip_reason: str,
    cooldown_required: float = 0,
    cooldown_elapsed: float = 0,
) -> None:
    """记录 ASE 每次决策循环结果（speak 或 skip）。"""
    _write(_ASE_LOG, "ase_check", {
        "session_id": session_id,
        "mode": mode,
        "urgency": urgency,
        "threshold": threshold,
        "silent_seconds": silent_seconds,
        "decision": decision,
        "skip_reason": skip_reason,
        "cooldown_required": cooldown_required,
        "cooldown_elapsed": cooldown_elapsed,
    })


def log_ase_speak(
    session_id: str,
    mode: str,
    urgency: float,
    vlm_used: bool,
    vlm_description_len: int,
    response_chars: int,
    duration_ms: int,
) -> None:
    """记录一次成功完成的主动发言。"""
    _write(_ASE_LOG, "ase_speak", {
        "session_id": session_id,
        "mode": mode,
        "urgency": urgency,
        "vlm_used": vlm_used,
        "vlm_description_len": vlm_description_len,
        "response_chars": response_chars,
        "duration_ms": duration_ms,
    })


def log_ase_error(session_id: str, error: str) -> None:
    """记录 ASE 主动发言时出现的异常。"""
    _write(_ASE_LOG, "ase_error", {
        "session_id": session_id,
        "error": error,
    })


# ── vlm.log（图像识别结构化事件）──────────────────────────────────────────────

def log_vlm_request(
    session_id: str,
    source: str,
    image_bytes: int,
    model: str,
) -> None:
    """记录 POST /vlm 请求（chat 或 ase 来源）。"""
    _write(_VLM_LOG, "vlm_request", {
        "session_id": session_id,
        "source": source,
        "image_bytes": image_bytes,
        "model": model,
    })


def log_vlm_result(
    session_id: str,
    source: str,
    description_len: int,
    description_preview: str,
    duration_ms: int,
) -> None:
    """记录 VLM 描述生成成功。"""
    _write(_VLM_LOG, "vlm_result", {
        "session_id": session_id,
        "source": source,
        "description_len": description_len,
        "description_preview": description_preview[:150],
        "duration_ms": duration_ms,
    })


def log_vlm_error(session_id: str, source: str, error: str) -> None:
    """记录 VLM 调用失败。"""
    _write(_VLM_LOG, "vlm_error", {
        "session_id": session_id,
        "source": source,
        "error": error,
    })


def log_vlm_screenshot_upload(
    session_id: str,
    image_bytes: int,
    triggered_by: str,
) -> None:
    """记录前端上传截图到 POST /sessions/{id}/screenshot。triggered_by: ase | user"""
    _write(_VLM_LOG, "vlm_screenshot_upload", {
        "session_id": session_id,
        "image_bytes": image_bytes,
        "triggered_by": triggered_by,
    })


# ── secondary_llm_calls.log / secondary_llm_responses.log ────────────────────
# Mirrors log_llm_call / log_llm_response for secondary (small) models.
# role: "emotion" | "affinity" | "reflection" | "memory_extract" |
#       "memory_summary" | "vlm" | "wizard" | "prompt_autofill"

def log_secondary_llm_call(
    role: str,
    messages: List[Dict[str, str]],
    model: str,
    gen_kwargs: Dict[str, Any],
    session_id: str = "",
) -> None:
    """Record the full prompt sent to a secondary LLM (non-streaming snapshots)."""
    _write(_SECONDARY_LLM_CALLS_LOG, "secondary_llm_call", {
        "role": role,
        "session_id": session_id,
        "model": model,
        "gen_kwargs": gen_kwargs,
        "message_count": len(messages),
        "messages": messages,
    })


def log_secondary_llm_response(
    role: str,
    response: str,
    model: str,
    session_id: str = "",
    duration_ms: int = 0,
) -> None:
    """Record the raw text returned by a secondary LLM."""
    _write(_SECONDARY_LLM_RESPONSES_LOG, "secondary_llm_response", {
        "role": role,
        "session_id": session_id,
        "model": model,
        "response": response,
        "char_count": len(response),
        "duration_ms": duration_ms,
    })


# ── ui_debug.log ──────────────────────────────────────────────────────────────

def log_ui_debug(prefix: str, msg: str, data: Dict[str, Any] = None,
                 level: str = "debug") -> None:
    """Record a frontend debug event forwarded via POST /debug/ui_log."""
    _write(_UI_DEBUG_LOG, "ui_debug", {
        "level": level,
        "prefix": prefix,
        "msg": msg,
        "data": data or {},
    })


# ── tools.log（工具输入/输出详情）────────────────────────────────────────────

def log_tool_timer(
    action: str,
    *,
    timer_id: str = "",
    label: str = "",
    seconds: int = 0,
    session_id: str = "",
    remaining_seconds: int = -1,
    error: str = "",
) -> None:
    """写入 tools.log：计时器工具操作详情。
    action: start | stop | expire | query
    remaining_seconds: 仅 query 时有值（-1 表示不适用）。
    """
    payload: Dict[str, Any] = {
        "action": action,
        "timer_id": timer_id,
        "label": label,
        "seconds": seconds,
        "session_id": session_id,
    }
    if remaining_seconds >= 0:
        payload["remaining_seconds"] = remaining_seconds
    if error:
        payload["error"] = error
    _write(_TOOLS_LOG, "tool_timer", payload)


def log_tool_todo(
    action: str,
    *,
    session_id: str = "",
    todo_id: str = "",
    content: str = "",
    done: Optional[bool] = None,
    priority: int = 0,
    removed_count: int = 0,
    error: str = "",
) -> None:
    """写入 tools.log：待办事项工具操作详情。
    action: create | update | delete | list | clear_done
    """
    payload: Dict[str, Any] = {
        "action": action,
        "session_id": session_id,
        "todo_id": todo_id,
        "content_preview": content[:100],
    }
    if done is not None:
        payload["done"] = done
    if priority:
        payload["priority"] = priority
    if removed_count:
        payload["removed_count"] = removed_count
    if error:
        payload["error"] = error
    _write(_TOOLS_LOG, "tool_todo", payload)


def log_tool_websearch_request(
    query: str,
    engine: str,
    max_results: int,
    session_id: str = "",
) -> None:
    """写入 tools.log：WebSearch 搜索请求（每次 search() 调用各引擎时触发）。
    engine: serper | ddg
    """
    _write(_TOOLS_LOG, "tool_websearch_request", {
        "query": query,
        "engine": engine,
        "max_results": max_results,
        "session_id": session_id,
    })


def log_tool_websearch_result(
    query: str,
    engine: str,
    result_count: int,
    results_preview: List[Dict[str, str]],
    duration_ms: float,
    error: str = "",
) -> None:
    """写入 tools.log：WebSearch 搜索结果详情。
    results_preview: 取前 3 条 [{title, snippet, url}]，snippet 截至 120 字。
    """
    trimmed = [
        {
            "title": r.get("title", "")[:80],
            "snippet": r.get("snippet", "")[:120],
            "url": r.get("url", "")[:200],
        }
        for r in results_preview[:3]
    ]
    _write(_TOOLS_LOG, "tool_websearch_result", {
        "query": query,
        "engine": engine,
        "result_count": result_count,
        "results_preview": trimmed,
        "duration_ms": round(duration_ms, 1),
        "error": error,
    })


def log_tool_trend(
    action: str,
    *,
    profile_id: str = "",
    sources_count: int = 0,
    added_count: int = 0,
    total_unused: int = 0,
    # per-source fields (used when action == "source")
    source_label: str = "",
    source_type: str = "",
    source_url: str = "",
    items_fetched: int = 0,
    items_added: int = 0,
    error: str = "",
) -> None:
    """写入 tools.log：趋势感知工具操作详情。
    action="fetch_start"  — 开始一次完整抓取
    action="source"       — 单个数据源的抓取结果
    action="fetch_done"   — 完整抓取结束汇总
    """
    payload: dict = {"action": action, "profile_id": profile_id, "error": error}
    if action == "source":
        payload.update({
            "source_label": source_label,
            "source_type": source_type,
            "source_url": source_url,
            "items_fetched": items_fetched,
            "items_added": items_added,
        })
    else:
        payload.update({
            "sources_count": sources_count,
            "added_count": added_count,
            "total_unused": total_unused,
        })
    _write(_TOOLS_LOG, "tool_trend", payload)


def log_tool_weather(
    action: str,
    *,
    profile_id: str = "",
    city: str = "",
    temp_c: str = "",
    description: str = "",
    error: str = "",
) -> None:
    """写入 tools.log：天气感知工具操作详情。action: fetch"""
    _write(_TOOLS_LOG, "tool_weather", {
        "action": action,
        "profile_id": profile_id,
        "city": city,
        "temp_c": temp_c,
        "description": description,
        "error": error,
    })


def log_affinity_entry(entry: Dict[str, Any]) -> None:
    """写入 affinity.log：一行一个 JSON 对象。
    建议字段：timestamp, session_id, turn_count；
    之前：old_affinity, old_status, old_status_description；
    输入：input_text（给 LLM 分析的对话文本）；
    LLM 输出：delta（好感度加减）, reason（加减原因）；
    之后：new_affinity, new_status, new_status_description。"""
    try:
        _ensure_dir(_AFFINITY_LOG)
        _rotate(_AFFINITY_LOG)
        if "timestamp" not in entry:
            entry = {"timestamp": time.time(), **entry}
        with open(_AFFINITY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
