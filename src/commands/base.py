"""命令系统基础 — CommandResult dataclass + dispatch() 路由。

所有命令（除 /help）执行后返回 context_for_llm，
让 AI 用自然语言表达结果，而不是直接显示冷冰冰的列表。

工具注册制 (规则 0.5)：
    新工具通过 register_command() 注册，无需修改 dispatch 的 if/else 分支。
    dispatch_command() 优先查 _COMMAND_REGISTRY，再回落到内建分支（向后兼容）。
"""
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# ── 命令注册表 ─────────────────────────────────────────────────────────────────
_COMMAND_REGISTRY: Dict[str, Callable] = {}


def register_command(name: str, handler: Callable) -> None:
    """注册一个命令处理器。handler 签名：async (sub, args, session, app) -> CommandResult"""
    if name in _COMMAND_REGISTRY:
        logger.warning("[base] 命令 '%s' 已注册，覆盖", name)
    _COMMAND_REGISTRY[name] = handler
    logger.debug("[base] 注册命令 '/%s'", name)


@dataclass
class CommandResult:
    context_for_llm: str = ""      # 注入 system 消息，供 LLM 参考后开口
    direct_response: str = ""      # bypass_llm=True 时直接返回（/help 专用）
    bypass_llm: bool = False       # True → 跳过 LLM，直接输出 direct_response


async def dispatch_command(message: str, session, app) -> CommandResult:
    """解析 /command 并路由到对应处理器。"""
    msg = message.strip()
    if not msg.startswith("/"):
        return CommandResult()

    # 去掉 / 后分词
    body = msg[1:].strip()
    parts = body.split(None, 2)   # 最多 3 段：cmd sub args
    if not parts:
        return CommandResult()

    cmd = parts[0].lower()
    sub = parts[1].lower() if len(parts) > 1 else ""
    args = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "")

    try:
        # 优先查注册表（新工具）
        if cmd in _COMMAND_REGISTRY:
            return await _COMMAND_REGISTRY[cmd](sub, args, session, app)

        # 内建分支（向后兼容）
        # /fact <sub> [args]
        if cmd == "fact":
            from src.commands.fact_cmds import handle_fact
            return await handle_fact(sub, args, session, app)

        # /recall <query>
        if cmd == "recall":
            query = body[len("recall"):].strip()
            from src.commands.memory_cmds import handle_recall
            return await handle_recall(query, session, app)

        # /memory status
        if cmd == "memory" and sub == "status":
            from src.commands.memory_cmds import handle_memory_status
            return await handle_memory_status(session, app)

        # /todo <sub> [args]
        if cmd == "todo":
            from src.commands.todo_cmds import handle_todo
            return await handle_todo(sub, args, session, app)

        # /topic [来源] — 让 AI 立刻主动起一个新话题
        if cmd == "topic":
            from src.commands.topic_cmds import handle_topic
            return await handle_topic(sub, args, session, app)

        # /help
        if cmd == "help":
            from src.commands.memory_cmds import handle_help
            return handle_help()

        # 未知命令 — 告知 LLM
        return CommandResult(
            context_for_llm=(
                f"[命令执行结果 · {msg}]\n"
                f"未知命令「/{cmd}」。请告知用户可用命令：/fact list, /fact add, "
                f"/fact delete, /recall, /memory status, /todo, /topic, /timer, /help"
            )
        )

    except Exception as e:
        logger.error("[dispatch_command] 命令处理失败 '%s': %s", msg, e)
        return CommandResult(
            context_for_llm=f"[命令执行结果 · {msg}]\n命令执行出错：{e}"
        )
