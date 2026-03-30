import asyncio
import json
import logging
import time
import traceback
from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.config.effective_config import get_effective_max_history_turns
from src.core.broadcast import broadcast
from src.prompt.pipeline import build_messages
from src.llm.registry import get_provider
from src.utils.debug_logger import (
    log_chat_request,
    log_llm_call,
    log_llm_response,
    log_error,
    log_debug,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    client_id: str = ""   # sender UUID — other clients use this to skip duplicates


@router.post("/chat")
async def chat_endpoint(request: Request, body: ChatRequest):
    sm = request.app.state.session_manager
    config = request.app.state.config

    session = sm.get_current()

    async def generate():
        session_id = session.id if session else None
        streaming_set = getattr(request.app.state, "chat_streaming_sessions", None)
        if streaming_set is None:
            request.app.state.chat_streaming_sessions = set()
            streaming_set = request.app.state.chat_streaming_sessions
        if session_id:
            streaming_set.add(session_id)
        try:
            if session is None:
                yield f"data: {json.dumps({'error': 'No active session', 'done': True})}\n\n"
                return

            preset = config.get_active_llm_preset()
            if not preset.get("api_key") and not preset.get("base_url"):
                yield f"data: {json.dumps({'error': 'LLM not configured. Set api_key and base_url in config/app.yaml', 'done': True})}\n\n"
                return

            store = session.conversation_store
            # 确保 MemoryManager 在 build_messages 之前存在（segments 需要它）
            from src.api.memory import _get_or_create_manager as _ensure_mem_mgr
            _ensure_mem_mgr(request, session.profile_id)

            n_turns = get_effective_max_history_turns(request.app, session.profile_id)
            build_extras = {}
            messages = build_messages(
                session,
                body.message,
                store,
                n_history_turns=n_turns,
                preset=preset,
                app=request.app,
                out_extras=build_extras,
            )
            llm = get_provider(preset)

            # ── log: incoming user turn ──────────────────────────────
            log_chat_request(
                session_id=session.id,
                session_name=session.display_name,
                user_message=body.message,
            )

            # ── P4: 好感度计数（每条消息 +1，不调 LLM）────────────────────────────
            affinity_engine = getattr(request.app.state, "affinity_engine", None)
            if affinity_engine:
                affinity_engine.increment_interaction(session)

            # Track last user message time (used by ReflectionEngine for silent_seconds)
            session.last_user_message_time = time.time()
            # 同步更新全局时间戳：只要用户在任何 session 发了消息，所有 session 的
            # 沉默计时都应重置，防止切换人物卡时误触发 ASE
            request.app.state.last_user_message_time = time.time()
            session.save_runtime_state()

            # cadence 记录（同步写内存，极快）
            _cadence = getattr(request.app.state, "cadence_tracker", None)
            if _cadence:
                _cadence.record(session.profile_id)

            # Record user message（带 sender，群聊防污染）
            user_name = getattr(config, "user_name", "用户") or "用户"
            store.append("user", body.message, sender=user_name)
            await broadcast.push(session.id, {
                "type": "new_message",
                "role": "user",
                "content": body.message,
                "timestamp": time.time(),
                "client_id": body.client_id,
                "sender": user_name,
            })

            # ── 双轨工具检测（消息保存后、LLM 调用前）───────────────────────────

            user_msg = body.message.strip()

            # 轨道1：NL触发器（仅非 / 命令消息）
            if not user_msg.startswith("/"):
                try:
                    from src.commands.nl_triggers import get_nl_triggers
                    for trigger in get_nl_triggers():
                        if trigger.detect(user_msg):
                            nl_result = await trigger.execute(user_msg, session, request.app)
                            if nl_result and nl_result.context_for_llm:
                                _inject_context(messages, nl_result.context_for_llm)
                except Exception as e:
                    logger.warning("[chat] NL trigger 失败: %s", e)

            # 轨道2：显式 /命令（仅 / 前缀）
            if user_msg.startswith("/"):
                try:
                    from src.commands.base import dispatch_command
                    cmd_result = await dispatch_command(user_msg, session, request.app)
                    if cmd_result.bypass_llm:
                        await broadcast.push(session.id, {
                            "type": "new_message",
                            "role": "assistant",
                            "content": cmd_result.direct_response,
                            "timestamp": time.time(),
                            "client_id": body.client_id,
                            "sender": session.display_name or "",
                        })
                        yield f"data: {json.dumps({'token': cmd_result.direct_response, 'done': False})}\n\n"
                        yield f"data: {json.dumps({'token': '', 'done': True})}\n\n"
                        return
                    elif cmd_result.context_for_llm:
                        _inject_context(messages, cmd_result.context_for_llm)
                except Exception as e:
                    logger.warning("[chat] /命令处理失败: %s", e)

            # ── log: full LLM payload ─────────────────────────────────
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

            full_response = ""
            llm_stream_start = time.perf_counter()
            try:
                async for token in llm.stream_chat(messages):
                    full_response += token
                    yield f"data: {json.dumps({'token': token, 'done': False})}\n\n"

                # ── debug: build + LLM 耗时写入 build_timings.log ─────────────
                llm_stream_ms = (time.perf_counter() - llm_stream_start) * 1000
                try:
                    from src.prompt.pipeline import get_last_build_timings
                    from src.utils.debug_logger import log_build_timings
                    log_build_timings(
                        session.id,
                        get_last_build_timings(),
                        llm_stream_ms,
                        model=preset.get("model", ""),
                    )
                except Exception as _e:
                    logger.debug("[chat] log_build_timings: %s", _e)

                # Strip markdown noise common in some LLMs
                full_response = full_response.replace("**", "").strip()

                # 空或 LLM 错误串：不存为正常回复、不跑后续引擎，前端显示固定提示；真实错误写入 app.log + debug.log 便于排查
                is_error_or_empty = not full_response or full_response.startswith("[LLM Error:")
                if is_error_or_empty:
                    reason = "llm_error" if full_response and full_response.startswith("[LLM Error:") else "empty"
                    actual_error = (full_response or "(empty)").strip()
                    if not actual_error:
                        actual_error = "empty response"
                    log_debug(
                        "chat_llm_empty_or_error",
                        source="chat",
                        session_id=session.id,
                        model=preset.get("model", ""),
                        reason=reason,
                        full_response=full_response or "(empty)",
                    )
                    log_error(
                        "chat/generate",
                        actual_error,
                        {"session_id": session.id, "response_preview": (full_response or "(empty)")[:500], "reason": reason},
                    )
                    store.append("assistant", "", sender=session.display_name or "")
                    await broadcast.push(session.id, {
                        "type": "new_message",
                        "role": "assistant",
                        "content": "回复生成失败，请重试。",
                        "timestamp": time.time(),
                        "client_id": body.client_id,
                        "sender": session.display_name or "",
                    })
                    log_llm_response(session_id=session.id, response=full_response or "(empty)", model=preset.get("model", ""))
                    yield f"data: {json.dumps({'token': '', 'done': True, 'error': 'empty or error response'})}\n\n"
                else:
                    store.append("assistant", full_response, sender=session.display_name or "")

                # ── P4: 情绪分类 + 好感度 LLM（fire-and-forget）──────────────────
                if not is_error_or_empty:
                    emotion_engine = getattr(request.app.state, "emotion_engine", None)
                    if emotion_engine:
                        from src.config.effective_config import get_effective_engine_config
                        cost = get_effective_engine_config(request.app, session.profile_id, "energy").get("message_cost", 2.0)
                        try:
                            cost = max(0.0, min(50.0, float(cost)))
                        except (TypeError, ValueError):
                            cost = 2.0
                        emotion_engine.apply_message_cost(session, cost=cost)
                        recent_ai = store.get_recent_ai_messages(3)
                        asyncio.create_task(_fire_task(
                            lambda: emotion_engine.maybe_classify(session, recent_ai, request.app),
                            "emotion_classify",
                        ))
                    if affinity_engine:
                        from src.config.effective_config import get_effective_engine_config
                        from src.config.profile_loader import ProfileLoader
                        freq = get_effective_engine_config(request.app, session.profile_id, "affinity").get("llm_adjust_frequency", 5)
                        try:
                            _profile = ProfileLoader().load(session.profile_id)
                        except Exception:
                            _profile = {}
                        mem = (_profile.get("memory_config") or {})
                        if mem.get("group_chat_merge_into_history") is not False and getattr(getattr(request.app, "state", None), "group_manager", None):
                            from src.memory.merged_history import get_merged_recent_messages
                            _merged = get_merged_recent_messages(session, request.app, _profile, freq, cap=None)
                            recent_turns = []
                            for m in _merged:
                                role, content, sender = m.get("role", "user"), (m.get("content") or "").strip(), (m.get("sender") or "").strip()
                                if m.get("source") == "group":
                                    content = f"【群「{m.get('gname', '')}」】{sender}: {content}"
                                recent_turns.append({"role": role, "content": content, "sender": sender})
                        else:
                            recent_turns = store.get_recent(freq)
                        _turns_aff = recent_turns
                        asyncio.create_task(_fire_task(
                            lambda: affinity_engine.maybe_llm_adjust(session, _turns_aff, request.app),
                            "affinity_adjust",
                        ))

                    # ── P6: 记忆 fire-and-forget 任务 ────────────────────────────────
                    try:
                        shown_ids = build_extras.get("shown_fact_ids") if isinstance(build_extras.get("shown_fact_ids"), (set, list)) else None
                        _fire_memory_tasks(session, store, request.app, shown_fact_ids=shown_ids)
                    except Exception as mem_err:
                        logger.warning("[chat] _fire_memory_tasks 失败: %s", mem_err)

                    # Broadcast completed assistant reply（带 sender）
                    await broadcast.push(session.id, {
                        "type": "new_message",
                        "role": "assistant",
                        "content": full_response,
                        "timestamp": time.time(),
                        "client_id": body.client_id,
                        "sender": session.display_name or "",
                    })

                    # ── log: completed assistant reply ───────────────────
                    log_llm_response(
                        session_id=session.id,
                        response=full_response,
                        model=preset.get("model", ""),
                    )

                    yield f"data: {json.dumps({'token': '', 'done': True})}\n\n"

            except (GeneratorExit, asyncio.CancelledError):
                logger.info("[chat] 流被客户端中断，停止生成")
                raise
            except Exception as e:
                logger.error(f"[chat] generation error: {e}")
                log_error("chat/generate", str(e), {"session_id": session.id}, traceback_str=traceback.format_exc())
                yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"
        finally:
            if session_id:
                streaming_set.discard(session_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _fire_task(fn, name: str = "task", retries: int = 1,
                    retry_delay: float = 5.0) -> None:
    """Fire-and-forget 包装器，支持重试。
    fn：返回 coroutine 的零参数 callable（请用 lambda 包装）。
    内层函数自己处理的 LLM 异常不会传播到这里；
    这一层捕获的是真正漏网的未处理异常，重试一次。
    """
    for attempt in range(1 + retries):
        try:
            await fn()
            return
        except Exception as e:
            if attempt < retries:
                logger.warning(
                    "[fire_task:%s] attempt %d/%d failed: %s — retrying in %.0fs",
                    name, attempt + 1, 1 + retries, e, retry_delay,
                )
                await asyncio.sleep(retry_delay)
            else:
                logger.error(
                    "[fire_task:%s] failed after %d attempt(s): %s",
                    name, 1 + retries, e, exc_info=True,
                )
                log_error("fire_task", str(e), {"task": name, "attempts": 1 + retries}, traceback_str=traceback.format_exc())


def _inject_context(messages: list, context: str) -> None:
    """将命令/触发器的上下文合并到第一条 system 消息末尾，避免产生多余的 system message。"""
    for msg in messages:
        if msg.get("role") == "system":
            msg["content"] = msg["content"] + "\n\n" + context
            return
    # 若尚无 system message（不应发生），插到最前
    messages.insert(0, {"role": "system", "content": context})


class SystemTriggerRequest(BaseModel):
    message: str  # 触发器注入的上下文（不存入历史）


@router.post("/sessions/{session_id}/system_trigger")
async def system_trigger_endpoint(session_id: str, body: SystemTriggerRequest,
                                  request: Request):
    """计时器到期、系统通知等场景：向 AI 注入一条系统上下文，获取 AI 自然语言回复并保存。
    触发消息本身不存入用户历史，只保存 AI 回复。"""
    sm = request.app.state.session_manager
    config = request.app.state.config

    session = sm.get_by_id(session_id)
    if session is None:
        session = sm.get_current()

    async def generate():
        if session is None:
            yield f"data: {json.dumps({'error': 'Session not found', 'done': True})}\n\n"
            return

        preset = config.get_active_llm_preset()
        if not preset.get("api_key") and not preset.get("base_url"):
            yield f"data: {json.dumps({'error': 'LLM not configured', 'done': True})}\n\n"
            return

        store = session.conversation_store

        from src.api.memory import _get_or_create_manager as _ensure_mem_mgr
        _ensure_mem_mgr(request, session.profile_id)

        n_turns = get_effective_max_history_turns(request.app, session.profile_id)
        # 构建历史上下文（空 user_msg），移除末尾空 user 占位
        messages = build_messages(
            session, "", store,
            n_history_turns=n_turns,
            preset=preset, app=request.app,
        )
        if messages and messages[-1].get("role") == "user" and not messages[-1].get("content", "").strip():
            messages.pop()
        # 触发文本作为 user 消息追加到末尾，LLM 以此为基础生成回复
        messages.append({"role": "user", "content": body.message})

        llm = get_provider(preset)
        gen_kwargs = {
            k: preset.get(k)
            for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty")
            if preset.get(k) is not None
        }
        log_llm_call(messages=messages, model=preset.get("model", ""),
                     gen_kwargs=gen_kwargs, session_id=session.id)

        full_response = ""
        try:
            async for token in llm.stream_chat(messages):
                full_response += token
                yield f"data: {json.dumps({'token': token, 'done': False})}\n\n"

            full_response = full_response.replace("**", "").strip()
            is_error_or_empty = not full_response or full_response.startswith("[LLM Error:")
            if is_error_or_empty:
                reason = "llm_error" if full_response and full_response.startswith("[LLM Error:") else "empty"
                actual_error = (full_response or "(empty)").strip() or "empty response"
                log_debug(
                    "chat_llm_empty_or_error",
                    source="system_trigger",
                    session_id=session.id,
                    model=preset.get("model", ""),
                    reason=reason,
                    full_response=full_response or "(empty)",
                )
                log_error(
                    "system_trigger/generate",
                    actual_error,
                    {"session_id": session.id, "response_preview": (full_response or "(empty)")[:500], "reason": reason},
                )
                store.append("assistant", "", sender=session.display_name or "")
                await broadcast.push(session.id, {
                    "type": "new_message",
                    "role": "assistant",
                    "content": "回复生成失败，请重试。",
                    "timestamp": time.time(),
                    "client_id": "",
                    "sender": session.display_name or "",
                })
                log_llm_response(session_id=session.id, response=full_response or "(empty)", model=preset.get("model", ""))
                yield f"data: {json.dumps({'token': '', 'done': True, 'error': 'empty or error response'})}\n\n"
            else:
                store.append("assistant", full_response, sender=session.display_name or "")
                emotion_engine = getattr(request.app.state, "emotion_engine", None)
                if emotion_engine:
                    from src.config.effective_config import get_effective_engine_config
                    cost = get_effective_engine_config(request.app, session.profile_id, "energy").get("message_cost", 2.0)
                    try:
                        cost = max(0.0, min(50.0, float(cost)))
                    except (TypeError, ValueError):
                        cost = 2.0
                    emotion_engine.apply_message_cost(session, cost=cost)
                    recent_ai = store.get_recent_ai_messages(3)
                    asyncio.create_task(_fire_task(
                        lambda: emotion_engine.maybe_classify(session, recent_ai, request.app),
                        "emotion_classify",
                    ))
                await broadcast.push(session.id, {
                    "type": "new_message",
                    "role": "assistant",
                    "content": full_response,
                    "timestamp": time.time(),
                    "client_id": "",
                    "sender": session.display_name or "",
                })
                log_llm_response(session_id=session.id, response=full_response,
                             model=preset.get("model", ""))
                yield f"data: {json.dumps({'token': '', 'done': True})}\n\n"

        except Exception as e:
            logger.error("[system_trigger] generation error: %s", e)
            log_error("system_trigger/generate", str(e), {"session_id": session.id}, traceback_str=traceback.format_exc())
            yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


def _fire_memory_tasks(session, store, app, shown_fact_ids=None):
    """启动 P6 记忆相关的 fire-and-forget 异步任务。shown_fact_ids 为本轮注入 prompt 的 fact id 集合，用于每日强化。"""
    # 本日被使用的 fact_id 写入 memory_meta（供每日遗忘任务强化/不衰减）
    if shown_fact_ids and session.profile_id:
        try:
            from datetime import datetime as _dt
            import json as _json
            import os as _os
            meta_path = _os.path.join(session.storage_root, "memory_meta.json")
            today = _dt.now().strftime("%Y-%m-%d")
            data = {}
            if _os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as _f:
                        data = _json.load(_f)
                except Exception:
                    pass
            by_date = data.get("used_fact_ids_by_date") or {}
            existing = set(by_date.get(today) or [])
            existing.update(shown_fact_ids)
            by_date[today] = list(existing)
            data["used_fact_ids_by_date"] = by_date
            _os.makedirs(_os.path.dirname(meta_path), exist_ok=True)
            with open(meta_path, "w", encoding="utf-8") as _f:
                _json.dump(data, _f, ensure_ascii=False, indent=2)
        except Exception as _e:
            logger.debug("[chat] 写入 used_fact_ids 失败: %s", _e)

    from src.api.memory import get_or_create_manager_for_session
    mgr = get_or_create_manager_for_session(app, session)
    if mgr is None:
        return

    if not mgr._cfg.get("enabled"):
        return

    # auto_extract
    try:
        from src.config.profile_loader import ProfileLoader
        profile = ProfileLoader().load(session.profile_id)
    except Exception:
        profile = {}

    # 读取当前情绪状态快照（18.1 情感记忆图谱），auto_extract 写入向量时会带 ai_emotion
    emotion_context: dict = {}
    try:
        import json as _json
        import os as _os
        _epath = _os.path.join("profiles", session.profile_id, "emotion_state.json")
        if _os.path.exists(_epath):
            with open(_epath, encoding="utf-8") as _f:
                emotion_context = _json.load(_f)
    except Exception:
        pass

    from src.utils.debug_logger import log_auto_extract_emotion_context
    log_auto_extract_emotion_context(
        session.profile_id,
        has_context=bool(emotion_context),
        primary_emotion=(emotion_context.get("primary_emotion") or ""),
    )

    freq = int(mgr._cfg.get("extraction_frequency", 5))
    mem = (profile.get("memory_config") or {})
    merge_enabled = mem.get("group_chat_merge_into_history") is not False and getattr(getattr(app, "state", None), "group_manager", None)
    if merge_enabled:
        from src.memory.merged_history import get_merged_recent_messages, get_merged_messages_for_date
        merged_recent = get_merged_recent_messages(session, app, profile, freq, cap=None)
        recent_turns = [{"role": m.get("role"), "content": m.get("content"), "sender": m.get("sender")} for m in merged_recent]
    else:
        recent_turns = store.get_recent(freq)
    # 用局部变量固定 lambda 闭包（避免循环/重用时变量逸出）
    _turns, _profile, _app, _ec = recent_turns, profile, app, emotion_context
    asyncio.create_task(_fire_task(
        lambda: mgr.auto_extract(_turns, _profile, _app, emotion_context=_ec),
        "memory_extract",
        retries=1,
        retry_delay=5.0,
    ))

    # day_summary：检测昨天有无对话+摘要（合并开启时用按日合并）
    if mgr._cfg.get("day_summary_enabled"):
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        if not mgr.day_store.has_summary_for(yesterday):
            if merge_enabled:
                yesterday_msgs = get_merged_messages_for_date(session, app, profile, yesterday)
                has_yesterday = bool(yesterday_msgs)
            else:
                all_msgs = store.get_all() if hasattr(store, "get_all") else []
                has_yesterday = any(
                    _msg_date(m) == yesterday
                    for m in all_msgs
                    if getattr(m, "role", None) in ("user", "assistant")
                )
            if has_yesterday:
                _d, _s, _p, _a = yesterday, store, profile, app
                if merge_enabled:
                    _override = yesterday_msgs  # 上面已算过
                    asyncio.create_task(_fire_task(
                        lambda: mgr.generate_day_summary(_d, _s, _p, _a, day_msgs_override=_override),
                        "day_summary",
                        retries=2,
                        retry_delay=10.0,
                    ))
                else:
                    asyncio.create_task(_fire_task(
                        lambda: mgr.generate_day_summary(_d, _s, _p, _a),
                        "day_summary",
                        retries=2,
                        retry_delay=10.0,
                    ))


def _msg_date(msg) -> str:
    ts = msg.get("timestamp") if isinstance(msg, dict) else getattr(msg, "timestamp", None)
    if ts:
        try:
            from datetime import datetime
            return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
        except Exception:
            pass
    return ""
