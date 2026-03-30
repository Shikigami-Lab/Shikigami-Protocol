"""Timer expire handler — 计时器到期时在后端直接触发 system_trigger。

流程：
  TimerManager._run_timer 到期
    → expire callback（后台线程）
    → asyncio.run_coroutine_threadsafe(_on_expire, loop)
    → build_messages + LLM + broadcast（完全在服务端，不依赖客户端在线）

前端只需响应 timer_expired SSE 事件刷新面板和显示 toast，不再调用 system_trigger。
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


async def _on_expire(app, timer_info: dict):
    """在 asyncio 事件循环中运行，复用 system_trigger 的核心逻辑。"""
    import json
    label = timer_info.get("label", "计时器")
    session_id = (timer_info.get("session_id") or "").strip()
    timer_id = timer_info.get("id", "")

    from src.utils.debug_logger import log_debug, log_error
    log_debug("timer_expire_backend_trigger", timer_id=timer_id, label=label, session_id=session_id)

    sm = app.state.session_manager
    config = app.state.config

    session = sm.get_by_id(session_id) if session_id else None
    if session is None:
        session = sm.get_current()
    if session is None:
        logger.warning("[timer_expire] 无活跃 session，跳过 system_trigger label=%s", label)
        return

    preset = config.get_active_llm_preset()
    if not preset.get("api_key") and not preset.get("base_url"):
        logger.warning("[timer_expire] LLM 未配置，跳过 label=%s", label)
        return

    from src.prompt.pipeline import build_messages
    from src.core.session_utils import get_effective_max_history_turns
    from src.llm.registry import get_provider
    from src.core.broadcast import broadcast

    store = session.conversation_store
    n_turns = get_effective_max_history_turns(app, session.profile_id)

    messages = build_messages(
        session, "", store,
        n_history_turns=n_turns,
        preset=preset, app=app,
    )
    # 移除末尾空 user 占位
    if messages and messages[-1].get("role") == "user" and not messages[-1].get("content", "").strip():
        messages.pop()

    trigger_msg = f"⏰ 你设置的计时器「{label}」已经到时了！"
    messages.append({"role": "user", "content": trigger_msg})

    llm = get_provider(preset)
    gen_kwargs = {
        k: preset.get(k)
        for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty")
        if preset.get(k) is not None
    }

    full_response = ""
    try:
        async for token in llm.stream_chat(messages, **gen_kwargs):
            full_response += token
            await broadcast.push(session.id, {"type": "token", "token": token, "done": False})

        full_response = full_response.replace("**", "").strip()
        if not full_response or full_response.startswith("[LLM Error:"):
            logger.warning("[timer_expire] LLM 回复为空或出错 label=%s", label)
            return

        store.append("assistant", full_response, sender=session.display_name or "")
        await broadcast.push(session.id, {
            "type": "new_message",
            "role": "assistant",
            "content": full_response,
            "done": True,
        })
        logger.info("[timer_expire] 回复完成 label=%s chars=%d", label, len(full_response))

    except Exception as e:
        log_error("timer/expire_handler", str(e), {"timer_id": timer_id, "label": label})
        logger.error("[timer_expire] 异常 label=%s: %s", label, e)


def make_expire_callback(app, loop: asyncio.AbstractEventLoop):
    """返回一个可注册到 TimerManager 的同步回调。"""
    def _callback(timer_info: dict):
        # 同时推送 timer_expired SSE（前端刷新面板 + toast）
        from src.core.broadcast import broadcast as _broadcast
        from src.utils.debug_logger import log_debug
        timer_id = timer_info.get("id", "")
        label = timer_info.get("label", "计时器")
        session_id = (timer_info.get("session_id") or "").strip()
        payload = {"type": "timer_expired", "label": label, "timer_id": timer_id}
        log_debug("timer_expire_callback", timer_id=timer_id, label=label, session_id=session_id)
        if session_id:
            asyncio.run_coroutine_threadsafe(
                _broadcast.push_one_for_session(session_id, payload), loop
            )
        else:
            asyncio.run_coroutine_threadsafe(_broadcast.push_one(payload), loop)

        # 后端直接触发 LLM 回复
        asyncio.run_coroutine_threadsafe(_on_expire(app, timer_info), loop)

    return _callback
