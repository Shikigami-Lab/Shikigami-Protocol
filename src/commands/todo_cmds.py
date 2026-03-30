"""/todo 命令处理器

/todo list              — 列出所有待办，AI 汇报未完成项
/todo add <内容>         — 新增一条待办
/todo complete <关键词>  — 按关键词模糊匹配标记为已完成
/todo delete <关键词>    — 按关键词模糊匹配删除
"""
import json
import logging
import os
import time
import uuid

from src.commands.base import CommandResult

logger = logging.getLogger(__name__)

_TODO_FILE = "todos.json"


# ── 存储辅助（与 api/todos.py 保持一致的轻量实现）─────────────────────────────

def _path(session) -> str:
    return os.path.join(session.storage_root, _TODO_FILE)


def _load(session) -> list:
    p = _path(session)
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(session, todos: list):
    p = _path(session)
    tmp = p + ".tmp"
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(todos, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception as e:
        logger.error("[todo_cmds] 保存失败: %s", e)


def _match(keyword: str, todos: list) -> list:
    """返回 content 包含 keyword 的待办列表（不区分大小写）。"""
    kw = keyword.lower()
    return [t for t in todos if kw in t.get("content", "").lower()]


# ── 命令处理器 ────────────────────────────────────────────────────────────────

async def handle_todo(sub: str, args: str, session, app) -> CommandResult:
    todos = _load(session)

    # /todo list
    if sub in ("list", "ls", "") or not sub:
        undone = [t for t in todos if not t.get("done")]
        done   = [t for t in todos if t.get("done")]
        if not todos:
            return CommandResult(
                context_for_llm=(
                    "[命令执行结果 · /todo list]\n"
                    "待办事项为空。请用自然语言告诉用户没有任何待办，可以带点轻松的语气。"
                )
            )
        lines = []
        for i, t in enumerate(undone, 1):
            lines.append(f"{i}. ☐ {t['content']}")
        for t in done:
            lines.append(f"  ✓ {t['content']}")
        body = "\n".join(lines)
        return CommandResult(
            context_for_llm=(
                f"[命令执行结果 · /todo list]\n"
                f"共 {len(todos)} 条待办（{len(undone)} 未完成，{len(done)} 已完成）：\n"
                f"{body}\n"
                "请根据以上列表，用自然语言告诉用户待办情况，重点提未完成项。"
            )
        )

    # /todo add <内容>
    if sub == "add":
        content = args.strip()
        if not content:
            return CommandResult(
                context_for_llm="[命令执行结果 · /todo add]\n没有提供内容。请告知用户用法：/todo add <内容>"
            )
        item = {
            "id": "td_" + uuid.uuid4().hex[:8],
            "content": content,
            "done": False,
            "created_at": time.time(),
        }
        todos.append(item)
        _save(session, todos)
        return CommandResult(
            context_for_llm=(
                f"[命令执行结果 · /todo add]\n"
                f"已新增待办：「{content}」。请用自然语言向用户确认已记下，可以带点认真负责的语气。"
            )
        )

    # /todo complete <关键词>
    if sub in ("complete", "done", "finish"):
        keyword = args.strip()
        if not keyword:
            return CommandResult(
                context_for_llm="[命令执行结果 · /todo complete]\n没有提供关键词。"
            )
        matches = _match(keyword, todos)
        undone_matches = [t for t in matches if not t.get("done")]
        if not undone_matches:
            return CommandResult(
                context_for_llm=(
                    f"[命令执行结果 · /todo complete]\n"
                    f"没有找到包含「{keyword}」的未完成待办。请自然地告诉用户。"
                )
            )
        for t in undone_matches:
            t["done"] = True
        _save(session, todos)
        names = "、".join(f"「{t['content']}」" for t in undone_matches)
        return CommandResult(
            context_for_llm=(
                f"[命令执行结果 · /todo complete]\n"
                f"已标记完成 {len(undone_matches)} 条：{names}。"
                "请用自然语言向用户确认，可以带点欣慰或鼓励的语气。"
            )
        )

    # /todo delete <关键词>
    if sub in ("delete", "del", "remove", "rm"):
        keyword = args.strip()
        if not keyword:
            return CommandResult(
                context_for_llm="[命令执行结果 · /todo delete]\n没有提供关键词。"
            )
        matches = _match(keyword, todos)
        if not matches:
            return CommandResult(
                context_for_llm=(
                    f"[命令执行结果 · /todo delete]\n"
                    f"没有找到包含「{keyword}」的待办。请自然地告诉用户。"
                )
            )
        ids_to_del = {t["id"] for t in matches}
        todos = [t for t in todos if t["id"] not in ids_to_del]
        _save(session, todos)
        names = "、".join(f"「{t['content']}」" for t in matches)
        return CommandResult(
            context_for_llm=(
                f"[命令执行结果 · /todo delete]\n"
                f"已删除 {len(matches)} 条：{names}。请用自然语言确认。"
            )
        )

    # 未知子命令
    return CommandResult(
        context_for_llm=(
            f"[命令执行结果 · /todo {sub}]\n"
            "未知的 /todo 子命令。可用：list / add <内容> / complete <关键词> / delete <关键词>"
        )
    )
