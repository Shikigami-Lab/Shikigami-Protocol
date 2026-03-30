import hashlib
import json
import logging
import os
import shutil
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from src.utils.debug_logger import log_session_switch, log_session_create, log_backup, log_error

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/sessions")
async def list_sessions(request: Request):
    sm = request.app.state.session_manager
    current = sm.get_current()
    current_id = current.id if current else None
    from src.config.profile_loader import ProfileLoader
    loader = ProfileLoader()
    sessions_data = []
    for s in sm.list_sessions():
        avatar = ""
        try:
            card = loader.load(s.profile_id)
            avatar = card.get("avatar", "")
        except Exception:
            pass
        sessions_data.append({
            "id": s.id,
            "profile_id": s.profile_id,
            "display_name": s.display_name,
            "is_current": s.id == current_id,
            "avatar": avatar,
        })
    return {"sessions": sessions_data, "current_id": current_id}


@router.get("/sessions/current")
async def get_current_session(request: Request):
    """轻量接口：仅返回服务端当前会话 id，用于多端一致性检测。"""
    sm = request.app.state.session_manager
    current = sm.get_current()
    return {"current_id": current.id if current else None}


class SwitchRequest(BaseModel):
    session_id: str


@router.post("/sessions/switch")
async def switch_session(request: Request, body: SwitchRequest):
    sm = request.app.state.session_manager
    current = sm.get_current()
    from_id = current.id if current else ""
    try:
        # 切换前清零旧 session 的 reflection urgency
        # 防止切回来时因旧状态立刻触发 ASE
        if current and hasattr(current, "reflection_state") and current.reflection_state:
            current.reflection_state["urgency"] = 0.0
            current.save_runtime_state()

        session = sm.switch(body.session_id)
        log_session_switch(
            from_id=from_id,
            to_id=session.id,
            to_name=session.display_name,
        )
        return {"ok": True, "session_id": session.id, "display_name": session.display_name}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/sessions/{session_id}/history")
async def clear_session_history(
    session_id: str,
    request: Request,
    type: Optional[str] = Query(None),
):
    """Clear the conversation history for a profile-session.

    type=None (default) — wipe everything.
    type=recent         — delete only the last 40 messages (clear short-term context).
    """
    sm = request.app.state.session_manager
    session = sm.get_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    store = session.conversation_store
    if type == "recent":
        # Non-destructive context reset: messages stay in the file (still visible
        # in the UI) but get_recent() will exclude them from the LLM context.
        # 同时重置该人格参与的所有群的短期上下文，与单聊+群聊合并语义一致。
        store.reset_context()
        gm = getattr(request.app.state, "group_manager", None)
        if gm and getattr(session, "profile_id", None):
            for gid in gm.get_group_ids_for_profile(session.profile_id):
                gstore = gm.get_conversation_store(gid)
                if gstore:
                    try:
                        gstore.reset_context()
                    except Exception:
                        pass
    else:
        store.clear()
    return {"ok": True}


@router.delete("/sessions/{session_id}/messages/from/{msg_idx}")
async def delete_messages_from(session_id: str, msg_idx: int, request: Request):
    """Delete all messages from msg_idx (inclusive) to the end."""
    sm = request.app.state.session_manager
    session = sm.get_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    store = session.conversation_store
    records = store.get_all()
    if 0 <= msg_idx < len(records):
        store.save_all(records[:msg_idx])
    return {"ok": True}


@router.delete("/sessions/{session_id}/messages/by-id/{msg_id}")
async def delete_message_by_id(session_id: str, msg_id: int, request: Request):
    """Delete a single message by its SQLite row id (used by memory panel chat history)."""
    sm = request.app.state.session_manager
    session = sm.get_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    store = session.conversation_store
    if hasattr(store, "delete_by_id") and store.delete_by_id(msg_id):
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Message not found")


@router.delete("/sessions/{session_id}/messages/{msg_idx}")
async def delete_message(session_id: str, msg_idx: int, request: Request):
    """Delete a single message by its position index (0-based, consistent with delete_messages_from)."""
    sm = request.app.state.session_manager
    session = sm.get_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    store = session.conversation_store
    records = store.get_all()
    if 0 <= msg_idx < len(records):
        store.save_all(records[:msg_idx] + records[msg_idx + 1:])
    return {"ok": True}


@router.post("/sessions/{session_id}/backup")
async def backup_session_memory(session_id: str, request: Request):
    """Create a timestamped backup of all memory files in the session's profile directory."""
    sm = request.app.state.session_manager
    session = sm.get_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join("backups", session_id, ts)
    copied = []
    errors = []

    # Back up the entire profile directory so any future file types are included automatically.
    # Exclude transient SQLite WAL/SHM files (they are empty or will be rolled back on restore).
    def _ignore(dir_path, names):
        return {n for n in names if n.endswith((".db-wal", ".db-shm"))}

    try:
        shutil.copytree(
            session.storage_root,
            backup_dir,
            ignore=_ignore,
            ignore_dangling_symlinks=True,
        )
        # Collect top-level file/dir names for the response
        for entry in os.scandir(backup_dir):
            copied.append(entry.name + ("/" if entry.is_dir() else ""))
    except Exception as e:
        err_msg = str(e)
        errors.append(err_msg)
        log_error("backup/copytree", err_msg, {"session_id": session_id})

    log_backup(session_id, backup_dir, copied, error="; ".join(errors))
    if errors and not copied:
        raise HTTPException(status_code=500, detail=f"备份失败: {'; '.join(errors)}")
    return {"ok": True, "path": backup_dir, "files": copied, "warnings": errors}


class SetSilenceBody(BaseModel):
    seconds_ago: float = 43200.0  # default 12h


@router.post("/sessions/{session_id}/debug/set_silence")
async def debug_set_silence(session_id: str, body: SetSilenceBody, request: Request):
    """Ablation test helper — backdates last_user_message_time in memory.

    Sets session.last_user_message_time (and app.state.last_user_message_time if the
    session is current) to time.time() - seconds_ago, so the reflection/ASE engines
    see the configured silence duration without waiting in real time.
    """
    sm = request.app.state.session_manager
    session = sm.get_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    backdated = time.time() - body.seconds_ago
    session.last_user_message_time = backdated

    current = sm.get_current()
    if current and current.id == session_id:
        request.app.state.last_user_message_time = backdated

    logger.info(
        "[debug/set_silence] %s last_user_message_time backdated %.0fs ago",
        session_id, body.seconds_ago,
    )
    return {"ok": True, "session_id": session_id, "seconds_ago": body.seconds_ago, "backdated_to": backdated}


class PromptBuildBody(BaseModel):
    """Dry-run the same prompt assembly as /chat (does not append messages to history)."""

    message: str = "【消融探针】请用一两句话描述你此刻的状态。"
    include_preview: bool = True
    preview_chars: int = 600


def _prompt_markers(system_text: str) -> Dict[str, bool]:
    """Heuristic flags for engine-driven blocks inside merged system message."""
    s = system_text or ""
    return {
        "emotion_state": ("当前情绪状态" in s) or ("Current emotional state" in s),
        "affinity_state": ("好感" in s) or ("Affinity" in s and "relationship" in s.lower()),
        "energy_state": ("能量" in s and "状态" in s) or ("Energy" in s and "level" in s.lower()),
        "reflection_state": ("自省" in s and "内心" in s) or ("reflection" in s.lower() and "thought" in s.lower()),
        "long_term_facts": "【长期认知与记忆】" in s or "long-term" in s.lower(),
        "mid_term_memory": ("日摘要" in s) or ("mid-term" in s.lower()),
    }


@router.post("/sessions/{session_id}/debug/prompt_build")
async def debug_prompt_build(session_id: str, request: Request, body: PromptBuildBody):
    """Stress-test / ablation: return segment timings + merged system stats without calling LLM.

    Does not mutate conversation store. Uses the same build_messages path as /chat.
    """
    from src.config.effective_config import get_effective_max_history_turns
    from src.prompt.pipeline import build_messages, get_last_build_timings

    sm = request.app.state.session_manager
    session = sm.get_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    config = request.app.state.config
    preset = config.get_active_llm_preset()
    store = session.conversation_store

    from src.api.memory import _get_or_create_manager as _ensure_mem_mgr

    _ensure_mem_mgr(request, session.profile_id)

    n_turns = get_effective_max_history_turns(request.app, session.profile_id)
    out_extras: Dict[str, Any] = {}
    messages = build_messages(
        session,
        body.message.strip() or "…",
        store,
        n_history_turns=n_turns,
        preset=preset,
        app=request.app,
        out_extras=out_extras,
    )

    timings = get_last_build_timings()
    seg_ms: Dict[str, float] = timings.get("segments") or {}
    segment_ids_fired: List[str] = sorted(seg_ms.keys())

    sys_content = ""
    for m in messages:
        if m.get("role") == "system":
            sys_content = m.get("content") or ""
            break

    sha256 = hashlib.sha256(sys_content.encode("utf-8")).hexdigest()
    n = max(100, min(body.preview_chars, 8000))
    preview: Dict[str, str] = {}
    if body.include_preview and sys_content:
        preview["head"] = sys_content[:n]
        if len(sys_content) > n:
            preview["tail"] = sys_content[-min(n, 400) :]

    return {
        "ok": True,
        "session_id": session_id,
        "profile_id": session.profile_id,
        "user_message": body.message,
        "segment_ids_fired": segment_ids_fired,
        "segments_ms": seg_ms,
        "system_length": len(sys_content),
        "system_sha256": sha256,
        "markers": _prompt_markers(sys_content),
        "profile_load_ms": timings.get("profile_load_ms"),
        "assemble_ms": timings.get("assemble_ms"),
        "total_build_ms": timings.get("total_build_ms"),
        "preview": preview,
    }
