"""Todo commands — /todo list | add | complete | delete"""
import logging

from src.commands.base import CommandResult, register_command
from src.tools.todo.store import TodoStore
from src.utils.debug_logger import log_todo, log_error

logger = logging.getLogger(__name__)


async def handle_todo(sub: str, args: str, session, app) -> CommandResult:
    store = TodoStore(session.storage_root)

    if sub in ("list", "ls", "查看", "列表", ""):
        items = store.get_all()
        pending = [t for t in items if not t.done]
        done = [t for t in items if t.done]
        lines = []
        for t in pending:
            lines.append(f"  [{t.id}] {t.content} (优先级{t.priority})")
        for t in done[:5]:
            lines.append(f"  [✓] {t.content}")
        summary = "\n".join(lines) if lines else "  暂无待办事项"
        return CommandResult(
            context_for_llm=(
                f"[待办事项]\n"
                f"未完成：{len(pending)} 件，已完成：{len(done)} 件\n"
                f"{summary}\n"
                f"请用自然的语气，像提醒朋友一样把待办事项告诉用户。"
            )
        )

    if sub in ("add", "新增", "添加", "+"):
        content = args.strip()
        if not content:
            return CommandResult(
                context_for_llm="[待办事项] 用法：/todo add <内容>。请提示用户需要输入内容。"
            )
        item = store.add(content)
        log_todo(session.id, "add", content=item.content, todo_id=item.id)
        return CommandResult(
            context_for_llm=(
                f"[待办事项·已添加]\n"
                f"新增：{item.content}（id: {item.id}）\n"
                f"[todo_updated]\n"
                f"请温暖地确认已记下这件事，一句话内完成。"
            )
        )

    if sub in ("complete", "done", "完成", "✓"):
        query = args.strip()
        if not query:
            return CommandResult(
                context_for_llm="[待办事项] 用法：/todo complete <关键词>。请提示用户需要输入关键词。"
            )
        item = store.complete(query)
        if item:
            log_todo(session.id, "complete", content=item.content, todo_id=item.id)
            return CommandResult(
                context_for_llm=(
                    f"[待办事项·已完成]\n"
                    f"完成：{item.content}\n"
                    f"[todo_updated]\n"
                    f"请给用户一句鼓励的话，为完成这件事庆祝一下。"
                )
            )
        return CommandResult(
            context_for_llm=f"[待办事项] 未找到包含「{query}」的待办事项。请告知用户。"
        )

    if sub in ("delete", "del", "rm", "删除", "移除"):
        query = args.strip()
        if not query:
            return CommandResult(
                context_for_llm="[待办事项] 用法：/todo delete <关键词>。请提示用户需要输入关键词。"
            )
        item = store.delete(query)
        if item:
            log_todo(session.id, "delete", content=item.content, todo_id=item.id)
            return CommandResult(
                context_for_llm=(
                    f"[待办事项·已删除]\n"
                    f"删除：{item.content}\n"
                    f"[todo_updated]\n"
                    f"请自然地确认已删除这件事。"
                )
            )
        return CommandResult(
            context_for_llm=f"[待办事项] 未找到包含「{query}」的待办事项。请告知用户。"
        )

    return CommandResult(
        context_for_llm=(
            f"[待办事项] 未知子命令「{sub}」。\n"
            f"可用：/todo list, /todo add <内容>, /todo complete <关键词>, /todo delete <关键词>"
        )
    )


# 模块加载时自动注册
register_command("todo", handle_todo)
