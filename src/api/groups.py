"""
groups.py — 群组 CRUD 与群聊历史 API（方案 B）

GET  /groups               — 群组列表
POST /groups               — 创建群组
GET  /groups/{group_id}    — 群组详情
GET  /groups/{group_id}/history — 群聊历史（与 session history 同 schema，带 sender）
POST /groups/{group_id}/chat — 群聊发言（用户一条消息，多 AI 按随机顺序依次回复，SSE 流）
DELETE /groups/{group_id}  — 删除群组
"""
import asyncio
import json
import logging
import random
import time
import traceback
from dataclasses import asdict
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.config.effective_config import get_effective_max_history_turns
from src.config.profile_loader import ProfileLoader
from src.core.broadcast import broadcast
from src.core.session import Session
from src.llm.registry import get_provider
from src.prompt.pipeline import build_messages
from src.utils.debug_logger import (
    log_chat_request,
    log_llm_call,
    log_llm_response,
    log_error,
    log_debug,
)

logger = logging.getLogger(__name__)
router = APIRouter()
_profile_loader = ProfileLoader()


class CreateGroupBody(BaseModel):
    display_name: str = "未命名群组"
    participants: List[dict] = []
    user_name: str = ""
    orchestrator: str = "random"
    max_replies_per_turn: int = 0  # 0 = 不限制，每轮最多 N 条 AI 回复（步骤6）


class GroupChatRequest(BaseModel):
    message: str
    client_id: str = ""


class UpdateGroupBody(BaseModel):
    display_name: str = ""
    participants: List[dict] = []
    max_replies_per_turn: int = -1  # -1 表示不修改，0 = 不限制
    orchestrator: Optional[str] = None  # None 不修改，random | fixed
    system_hint: Optional[str] = None  # 群专用 system 规则，None 不修改


def _get_group_manager(request: Request):
    mgr = getattr(request.app.state, "group_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="Group manager not initialized")
    return mgr


@router.get("/groups")
async def list_groups(request: Request):
    """返回所有群组列表（含 group_id、display_name、participants、orchestrator、created_at）。"""
    mgr = _get_group_manager(request)
    groups = mgr.list_groups()
    return {"groups": groups}


@router.post("/groups")
async def create_group(request: Request, body: CreateGroupBody):
    """
    创建群组。
    participants: [{ "profile_id", "type": "ai", "name" }]
    """
    mgr = _get_group_manager(request)
    display_name = (body.display_name or "").strip() or "未命名群组"
    participants = list(body.participants) if body.participants else []
    user_name = (body.user_name or "").strip() or None
    orchestrator = (body.orchestrator or "random").strip() or "random"
    cfg = mgr.create_group(
        display_name=display_name,
        participants=participants,
        user_name=user_name,
        orchestrator=orchestrator,
        max_replies_per_turn=getattr(body, "max_replies_per_turn", 0) or 0,
    )
    return cfg


@router.get("/groups/{group_id}")
async def get_group(request: Request, group_id: str):
    """返回单个群组配置。"""
    mgr = _get_group_manager(request)
    cfg = mgr.get_group(group_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")
    return cfg


@router.get("/groups/{group_id}/history")
async def get_group_history(
    request: Request,
    group_id: str,
    limit: int = Query(200, ge=0, description="Max messages to return (0 = all)"),
    offset: int = Query(0, ge=0, description="Skip this many from newest (for pagination)"),
):
    """返回群聊历史，与 GET /sessions/{id}/history 同 schema（含 sender）。offset=0 表示最新一页。"""
    mgr = _get_group_manager(request)
    if mgr.get_group(group_id) is None:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")
    store = mgr.get_conversation_store(group_id)
    if store is None:
        raise HTTPException(status_code=404, detail=f"Group store '{group_id}' not available")
    all_msgs = store.get_all_with_id()
    total = len(all_msgs)
    if limit <= 0:
        subset = all_msgs
    elif offset <= 0:
        subset = all_msgs[-limit:]
    else:
        end = total - offset
        start = max(0, end - limit)
        subset = all_msgs[start:end]
    return {"group_id": group_id, "total": total, "messages": subset}


@router.delete("/groups/{group_id}/messages/from/{msg_idx}")
async def delete_group_messages_from(
    request: Request, group_id: str, msg_idx: int
):
    """删除群聊中从 msg_idx（含）起的所有消息（与单聊 DELETE /sessions/.../messages/from 一致）。"""
    mgr = _get_group_manager(request)
    if mgr.get_group(group_id) is None:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")
    store = mgr.get_conversation_store(group_id)
    if store is None:
        raise HTTPException(status_code=404, detail=f"Group store '{group_id}' not available")
    records = store.get_all()
    if 0 <= msg_idx < len(records):
        store.save_all(records[:msg_idx])
    return {"ok": True}


@router.delete("/groups/{group_id}/messages/{msg_idx}")
async def delete_group_message(
    request: Request, group_id: str, msg_idx: int
):
    """删除群聊中第 msg_idx 条消息（与单聊 DELETE /sessions/.../messages/{idx} 一致）。"""
    mgr = _get_group_manager(request)
    if mgr.get_group(group_id) is None:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")
    store = mgr.get_conversation_store(group_id)
    if store is None:
        raise HTTPException(status_code=404, detail=f"Group store '{group_id}' not available")
    records = store.get_all()
    if 0 <= msg_idx < len(records):
        store.save_all(records[:msg_idx] + records[msg_idx + 1:])
    return {"ok": True}


@router.delete("/groups/{group_id}/messages/clear")
async def clear_group_messages(request: Request, group_id: str):
    """清空本群全部聊天记录，群组保留。"""
    mgr = _get_group_manager(request)
    if mgr.get_group(group_id) is None:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")
    store = mgr.get_conversation_store(group_id)
    if store is None:
        raise HTTPException(status_code=404, detail=f"Group store '{group_id}' not available")
    store.save_all([])
    return {"ok": True, "group_id": group_id}


@router.get("/groups/{group_id}/messages/search")
async def search_group_messages(
    request: Request,
    group_id: str,
    q: str = Query("", description="搜索关键词"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """搜索群内消息（LIKE），返回含 id, role, content, timestamp, sender。"""
    mgr = _get_group_manager(request)
    if mgr.get_group(group_id) is None:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")
    store = mgr.get_conversation_store(group_id)
    if store is None:
        raise HTTPException(status_code=404, detail=f"Group store '{group_id}' not available")
    items = store.search_messages(q=q, limit=limit, offset=offset)
    return {"group_id": group_id, "q": q, "items": items}


@router.get("/groups/{group_id}/history/export")
async def export_group_history(
    request: Request,
    group_id: str,
    format: str = Query("json", description="json | txt | md"),
):
    """导出群聊记录。format: json 返回 JSON 数组，txt/md 返回纯文本。"""
    mgr = _get_group_manager(request)
    if mgr.get_group(group_id) is None:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")
    store = mgr.get_conversation_store(group_id)
    if store is None:
        raise HTTPException(status_code=404, detail=f"Group store '{group_id}' not available")
    all_msgs = store.get_all()
    messages = [asdict(m) for m in all_msgs]
    fmt = (format or "json").strip().lower()
    if fmt == "json":
        from fastapi.responses import JSONResponse
        return JSONResponse(content={"group_id": group_id, "messages": messages})
    if fmt in ("txt", "md"):
        from fastapi.responses import PlainTextResponse
        from datetime import datetime
        lines = []
        for m in messages:
            ts = m.get("timestamp") or 0
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
            sender = (m.get("sender") or "").strip() or (m.get("role") == "user" and "用户" or "助手")
            content = (m.get("content") or "").replace("\r\n", "\n")
            lines.append(f"[{dt}] {sender}: {content}")
        body = "\n\n".join(lines)
        return PlainTextResponse(content=body, media_type="text/plain; charset=utf-8")
    raise HTTPException(status_code=400, detail="format must be json, txt, or md")


def _participant_display_name(participant: dict) -> str:
    """Participant display name: participant['name'] or profile card display_name."""
    name = (participant.get("name") or "").strip()
    if name:
        return name
    pid = participant.get("profile_id") or ""
    if not pid:
        return "AI"
    try:
        card = _profile_loader.load(pid)
        return (card.get("display_name") or pid).strip()
    except Exception:
        return pid


@router.post("/groups/{group_id}/chat")
async def group_chat(group_id: str, request: Request, body: GroupChatRequest):
    """
    群聊发言：用户发一条消息，参与群内的 AI 按随机顺序依次回复。
    历史写入该群 store，每条带 sender；SSE 流式返回，每条 AI 回复结束时带 sender。
    前端可订阅 GET /events/messages?session_id=group:{group_id} 接收广播。
    """
    mgr = _get_group_manager(request)
    cfg = mgr.get_group(group_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")
    store = mgr.get_conversation_store(group_id)
    if store is None:
        raise HTTPException(status_code=404, detail=f"Group store '{group_id}' not available")

    participants = list(cfg.get("participants") or [])
    participants = [p for p in participants if isinstance(p, dict) and p.get("profile_id") and not p.get("muted")]
    if not participants:
        raise HTTPException(status_code=400, detail="Group has no active AI participants (all may be muted)")

    config = getattr(request.app.state, "config", None)
    user_name = (cfg.get("user_name") or "").strip() or (
        getattr(config, "user_name", "用户") if config else "用户"
    )
    preset = config.get_active_llm_preset() if config else {}
    if not preset.get("api_key") and not preset.get("base_url"):
        raise HTTPException(
            status_code=503,
            detail="LLM not configured. Set api_key and base_url in config.",
        )

    # 用户消息写入群 store 并广播
    now = time.time()
    store.append("user", body.message, sender=user_name)
    channel = f"group:{group_id}"
    await broadcast.push(channel, {
        "type": "new_message",
        "role": "user",
        "content": body.message,
        "timestamp": now,
        "client_id": body.client_id,
        "sender": user_name,
    })

    # 群聊写入：每个参与人格的 last_user_message_time、cadence（与计划一致）
    setattr(request.app.state, "last_user_message_time", now)
    sm = getattr(request.app.state, "session_manager", None)
    cadence_tracker = getattr(request.app.state, "cadence_tracker", None)
    for participant in participants:
        profile_id = (participant.get("profile_id") or "").strip()
        if not profile_id:
            continue
        if sm:
            session = sm.get_by_id(profile_id)
            if session is not None:
                session.last_user_message_time = now
                session.save_runtime_state()
        if cadence_tracker:
            cadence_tracker.record(profile_id, now)

    # N = 本轮 AI 对话条数。按轮询交替（ABAB…）；orchestrator=fixed 不 shuffle，random 随机首序
    order = list(participants)
    if (cfg.get("orchestrator") or "random") == "random":
        random.shuffle(order)
    all_names = [_participant_display_name(p) for p in participants]
    system_hint = (cfg.get("system_hint") or "").strip()
    max_replies = int(cfg.get("max_replies_per_turn") or 0)
    reply_count = max_replies if max_replies > 0 else len(order)  # 0 = 每人一条
    reply_sequence = [order[i % len(order)] for i in range(reply_count)]

    async def generate():
        prev_sender: Optional[str] = None
        prev_content: Optional[str] = None
        for participant in reply_sequence:
            profile_id = participant.get("profile_id") or ""
            display_name = _participant_display_name(participant)
            session = sm.get_by_id(profile_id)
            if session is None:
                session = Session.from_profile(profile_id, display_name)
                session.load_runtime_state()
            n_turns = get_effective_max_history_turns(request.app, profile_id)
            extra_system = f"当前群聊参与者：{', '.join(all_names)}。你是 {display_name}，请以 {display_name} 的身份回复。"
            if system_hint:
                extra_system = system_hint + "\n\n" + extra_system
            last_override = None
            if prev_sender is not None and prev_content is not None:
                last_override = {"role": "user", "content": f"[{prev_sender}]: {prev_content}"}
            current_group_gname = (cfg.get("display_name") or group_id or "").strip() or group_id
            messages = build_messages(
                session,
                body.message,
                store,
                n_history_turns=n_turns,
                preset=preset,
                app=request.app,
                group_reply_as_sender=display_name,
                group_user_name=user_name,
                extra_system=extra_system,
                last_message_override=last_override,
                current_group_id=group_id,
                current_group_gname=current_group_gname,
            )
            llm = get_provider(preset)
            log_chat_request(
                session_id=group_id,
                session_name=f"group:{group_id}",
                user_message=body.message,
            )
            gen_kwargs = {
                k: preset.get(k)
                for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty")
                if preset.get(k) is not None
            }
            log_llm_call(
                messages=messages,
                model=preset.get("model", ""),
                gen_kwargs=gen_kwargs,
                session_id=group_id,
            )
            full_response = ""
            try:
                # 先发 sender，前端在收到第一个 token 前就能显示「正在说话的 AI」头像
                yield f"data: {json.dumps({'sender': display_name})}\n\n"
                try:
                    async for token in llm.stream_chat(messages):
                        full_response += token
                        yield f"data: {json.dumps({'token': token, 'done': False, 'sender': display_name})}\n\n"
                except Exception as e:
                    logger.error("[group_chat] LLM error for %s: %s", display_name, e)
                    log_error("group_chat/generate", str(e), {"group_id": group_id, "reply_as": display_name}, traceback_str=traceback.format_exc())
                    yield f"data: {json.dumps({'error': str(e), 'done': True, 'sender': display_name})}\n\n"
                    continue
                full_response = full_response.replace("**", "").strip()
                # 空或 LLM 错误串：真实错误写入 app.log + debug.log，不存为正常回复，前端显示固定提示
                is_error_or_empty = not full_response or full_response.startswith("[LLM Error:")
                if is_error_or_empty:
                    reason = "llm_error" if full_response and full_response.startswith("[LLM Error:") else "empty"
                    actual_error = (full_response or "(empty)").strip() or "empty response"
                    log_debug(
                        "chat_llm_empty_or_error",
                        source="group_chat",
                        group_id=group_id,
                        reply_as=display_name,
                        model=preset.get("model", ""),
                        reason=reason,
                        full_response=full_response or "(empty)",
                    )
                    log_error(
                        "group_chat/generate",
                        actual_error,
                        {"group_id": group_id, "reply_as": display_name, "response_preview": (full_response or "(empty)")[:500], "reason": reason},
                    )
                    store.append("assistant", "", sender=display_name)
                    log_llm_response(session_id=group_id, response=full_response or "(empty)", model=preset.get("model", ""))
                    await broadcast.push(channel, {
                        "type": "new_message",
                        "role": "assistant",
                        "content": "回复生成失败，请重试。",
                        "timestamp": time.time(),
                        "client_id": body.client_id,
                        "sender": display_name,
                    })
                    yield f"data: {json.dumps({'token': '', 'done': True, 'error': 'empty or error response', 'sender': display_name})}\n\n"
                    prev_sender = display_name
                    prev_content = ""
                else:
                    store.append("assistant", full_response, sender=display_name)
                    log_llm_response(session_id=group_id, response=full_response, model=preset.get("model", ""))
                    await broadcast.push(channel, {
                        "type": "new_message",
                        "role": "assistant",
                        "content": full_response,
                        "timestamp": time.time(),
                        "client_id": body.client_id,
                        "sender": display_name,
                    })
                # 该 AI 回复后的好感度与情绪（仅当前人格）；仅成功回复时执行
                if not is_error_or_empty:
                    affinity_engine = getattr(request.app.state, "affinity_engine", None)
                    if affinity_engine:
                        affinity_engine.increment_interaction(session)
                        from src.config.effective_config import get_effective_engine_config
                        freq = get_effective_engine_config(request.app, profile_id, "affinity").get("llm_adjust_frequency", 5)
                        recent_turns = store.get_recent(freq)
                        asyncio.create_task(_fire_task(
                            lambda: affinity_engine.maybe_llm_adjust(session, recent_turns, request.app),
                            "group_affinity_adjust",
                        ))
                    emotion_engine = getattr(request.app.state, "emotion_engine", None)
                    if emotion_engine:
                        from src.config.effective_config import get_effective_engine_config
                        cost = get_effective_engine_config(request.app, profile_id, "energy").get("message_cost", 2.0)
                        try:
                            cost = max(0.0, min(50.0, float(cost)))
                        except (TypeError, ValueError):
                            cost = 2.0
                        emotion_engine.apply_message_cost(session, cost=cost)
                        recent_ai = store.get_recent_ai_messages(3)
                        asyncio.create_task(_fire_task(
                            lambda: emotion_engine.maybe_classify(session, recent_ai, request.app),
                            "group_emotion_classify",
                        ))
                    # 群聊也触发记忆提取：该人格的 fact/向量写入（recent_turns 来自本群 store）
                    try:
                        from src.api.chat import _fire_memory_tasks
                        _fire_memory_tasks(session, store, request.app, shown_fact_ids=None)
                    except Exception as mem_err:
                        logger.warning("[group_chat] _fire_memory_tasks 失败: %s", mem_err)
                    prev_sender = display_name
                    prev_content = full_response
                    yield f"data: {json.dumps({'token': '', 'done': True, 'sender': display_name})}\n\n"
            except (GeneratorExit, asyncio.CancelledError):
                logger.info("[group_chat] 流被客户端中断，停止后续回复")
                raise
            except (ConnectionError, BrokenPipeError, OSError) as e:
                logger.info("[group_chat] 连接已关闭，停止后续回复: %s", e)
                break
            except Exception as e:
                logger.warning("[group_chat] generate 异常（可能为客户端断开）: %s", e)
                break
        # 全部回复结束
        try:
            yield f"data: {json.dumps({'token': '', 'done': True})}\n\n"
        except (GeneratorExit, asyncio.CancelledError, ConnectionError, BrokenPipeError, OSError):
            pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _fire_task(fn, name: str = "task", retries: int = 1, retry_delay: float = 5.0) -> None:
    """Fire-and-forget with retry (group chat affinity/emotion). fn() may return a coroutine."""
    for attempt in range(1 + retries):
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                await result
            return
        except Exception as e:
            if attempt < retries:
                logger.warning("[group_chat] %s attempt %d failed: %s", name, attempt + 1, e)
                await asyncio.sleep(retry_delay)
            else:
                logger.error("[group_chat] %s failed: %s", name, e, exc_info=True)
                log_error("group_chat/fire_task", str(e), {"task": name}, traceback_str=traceback.format_exc())


@router.patch("/groups/{group_id}")
async def update_group(request: Request, group_id: str, body: UpdateGroupBody):
    """更新群组：改名、更新参与者（邀请/剔除）。"""
    mgr = _get_group_manager(request)
    if mgr.get_group(group_id) is None:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")
    updates = {}
    if body.display_name is not None and (body.display_name or "").strip():
        updates["display_name"] = (body.display_name or "").strip()
    if body.participants is not None and isinstance(body.participants, list) and len(body.participants) > 0:
        updates["participants"] = [p for p in body.participants if isinstance(p, dict) and p.get("profile_id")]
    if getattr(body, "max_replies_per_turn", -1) >= 0:
        updates["max_replies_per_turn"] = max(0, int(body.max_replies_per_turn))
    if getattr(body, "orchestrator", None) is not None:
        updates["orchestrator"] = (body.orchestrator or "random").strip() or "random"
    if getattr(body, "system_hint", None) is not None:
        updates["system_hint"] = (body.system_hint or "").strip()
    if not updates:
        return mgr.get_group(group_id)
    cfg = mgr.update_group(group_id, updates)
    if cfg is None:
        raise HTTPException(status_code=500, detail="Failed to update group")
    return cfg


@router.delete("/groups/{group_id}")
async def delete_group(request: Request, group_id: str):
    """删除群组及其历史。"""
    mgr = _get_group_manager(request)
    if mgr.get_group(group_id) is None:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")
    ok = mgr.delete_group(group_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to delete group")
    return {"ok": True, "group_id": group_id}
