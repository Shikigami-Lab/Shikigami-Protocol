"""/fact 子命令处理器

/fact list [分类]   — 默认只列已置顶事实，最多显示 N 条，会提示还有多少条未显示
/fact list all     — 列全部事实（不受置顶与条数限制时仍截断并提示）
/fact add [分类:] 内容
/fact delete <序号|ID|关键词>
"""
import logging
from typing import Optional

from src.commands.base import CommandResult
from src.utils.debug_logger import log_fact_add, log_fact_delete

logger = logging.getLogger(__name__)

# 默认只展示前 N 条，避免刷屏；/fact list all 可看全部
LIST_DEFAULT_LIMIT = 20

# 分类别名映射
_CATEGORY_MAP = {
    "习惯": "habit", "habits": "habit", "habit": "habit",
    "偏好": "preference", "preferences": "preference", "preference": "preference",
    "禁忌": "taboo", "taboos": "taboo", "taboo": "taboo",
    "关系": "relationship", "relationships": "relationship", "relationship": "relationship",
    "位置": "location", "location": "location",
    "里程碑": "milestone", "milestone": "milestone",
    "洞察": "ai_insight", "ai_insight": "ai_insight",
    "其他": "other", "other": "other",
}


def _get_manager(session, app):
    managers = getattr(app.state, "memory_managers", {})
    return managers.get(session.profile_id)


async def handle_fact(sub: str, args: str, session, app) -> CommandResult:
    if sub in ("list", "列表", "ls", ""):
        return await handle_list(args.strip(), session, app)
    if sub in ("add", "添加", "记住", "记", "+"):
        return await handle_add(args.strip(), session, app)
    if sub in ("delete", "del", "rm", "删除", "忘记", "forget", "-"):
        return await handle_delete(args.strip(), session, app)
    # 默认：把 sub + args 作为 add 的内容
    return await handle_add((sub + " " + args).strip(), session, app)


async def handle_list(args: str, session, app) -> CommandResult:
    mgr = _get_manager(session, app)
    if not mgr:
        return CommandResult(context_for_llm="[命令执行结果 · /fact list]\n记忆系统尚未初始化。")

    facts = mgr.facts.get_all()
    if not facts:
        return CommandResult(
            context_for_llm=(
                "[命令执行结果 · /fact list]\n"
                "事实库目前是空的。请用自然语言表达这一情况，可以带一点遗憾或期待的情绪。"
            )
        )

    # 默认只列已置顶；/fact list all 或 /fact list 全部 → 列全部
    show_all = (args or "").strip().lower() in ("all", "全部")
    if show_all:
        category_filter = None
    else:
        category_filter = (args or "").strip()

    # 按 score 排序
    facts_sorted = sorted(facts, key=lambda f: f.score(), reverse=True)
    if not show_all:
        facts_sorted = [f for f in facts_sorted if f.pinned]
        if not facts_sorted:
            return CommandResult(
                context_for_llm=(
                    "[命令执行结果 · /fact list]\n"
                    "目前没有置顶的事实。请用自然口语告诉用户，并提示可以说「/fact list all」查看全部。"
                )
            )

    # 可选过滤分类
    if category_filter:
        cat = _CATEGORY_MAP.get(category_filter.lower(), category_filter.lower())
        if cat and cat in _CATEGORY_MAP.values():
            facts_sorted = [f for f in facts_sorted if f.category == cat]

    total = len(facts_sorted)
    facts_to_show = facts_sorted[:LIST_DEFAULT_LIMIT]
    displayed = len(facts_to_show)

    lines = []
    for i, f in enumerate(facts_to_show, 1):
        lines.append(f"{i}. [{f.category}] {f.content}  (来源:{f.source})")
    body = "\n".join(lines)

    hint = ""
    if total > displayed:
        hint = (
            f"\n（以上共显示 {displayed} 条，还有 {total - displayed} 条未显示。"
            "请用口语告诉用户：还有几条没列出来，想说「再列一些」或发 /fact list all 就能看全部。）"
        )

    scope_desc = "已置顶的" if not show_all else "记住的"
    return CommandResult(
        context_for_llm=(
            "[命令执行结果 · /fact list]\n"
            f"以下是{scope_desc}关于用户的事实，请用自然、符合角色的语言告诉用户，可以加入情感：\n"
            f"---\n{body}{hint}"
        )
    )


async def handle_add(content_raw: str, session, app) -> CommandResult:
    mgr = _get_manager(session, app)
    if not mgr:
        return CommandResult(context_for_llm="[命令执行结果 · /fact add]\n记忆系统尚未初始化。")

    if not content_raw:
        return CommandResult(
            context_for_llm=(
                "[命令执行结果 · /fact add]\n"
                "没有收到要记住的内容。请提示用户用法：/fact add [分类:] 内容"
            )
        )

    # 解析可选 "分类:" 前缀
    category = "other"
    content = content_raw
    if ":" in content_raw or "：" in content_raw:
        sep = ":" if ":" in content_raw else "："
        pre, _, rest = content_raw.partition(sep)
        pre_mapped = _CATEGORY_MAP.get(pre.strip().lower())
        if pre_mapped:
            category = pre_mapped
            content = rest.strip()

    fact = mgr.facts.add(content, category=category, source="manual")
    if fact is None:
        return CommandResult(
            context_for_llm=(
                "[命令执行结果 · /fact add]\n"
                f"这件事已经记住了（重复）：「{content}」\n"
                "请自然地告诉用户你已经知道这件事了。"
            )
        )

    log_fact_add(
        session.profile_id,
        fact.id,
        fact.content,
        fact.category,
        fact.source,
        weight=fact.weight,
        tags=fact.tags,
        pinned=fact.pinned,
        emotional_note=fact.emotional_note,
        updated_at=fact.updated_at,
        is_manual=fact.is_manual,
    )

    return CommandResult(
        context_for_llm=(
            f"[命令执行结果 · /fact add]\n"
            f"已成功记住：「{fact.content}」（分类：{fact.category}）\n"
            "请用自然、温暖的语气告诉用户这件事已被记住，可以稍加情感表达。"
        )
    )


async def handle_delete(target: str, session, app) -> CommandResult:
    mgr = _get_manager(session, app)
    if not mgr:
        return CommandResult(context_for_llm="[命令执行结果 · /fact delete]\n记忆系统尚未初始化。")

    if not target:
        return CommandResult(
            context_for_llm=(
                "[命令执行结果 · /fact delete]\n"
                "请告诉用户需要提供序号、ID 或关键词，例如：/fact delete 1"
            )
        )

    facts_sorted = sorted(mgr.facts.get_all(), key=lambda f: f.score(), reverse=True)

    # 纯数字 → 序号
    if target.isdigit():
        idx = int(target) - 1
        if 0 <= idx < len(facts_sorted):
            fact = facts_sorted[idx]
            log_fact_delete(session.profile_id, fact.id, fact.content, "command")
            mgr.facts.delete(fact.id)
            return CommandResult(
                context_for_llm=(
                    f"[命令执行结果 · /fact delete]\n"
                    f"已删除第{target}条事实：「{fact.content}」\n"
                    "请自然地告诉用户这件事已被遗忘，可以带一点失落或释怀的情绪。"
                )
            )
        return CommandResult(
            context_for_llm=(
                f"[命令执行结果 · /fact delete]\n"
                f"序号 {target} 超出范围（共 {len(facts_sorted)} 条）。请告知用户。"
            )
        )

    # 8位十六进制 ID
    if len(target) >= 8 and all(c in "0123456789abcdefABCDEF" for c in target[:8]):
        # 先在完整 ID 里找
        for f in facts_sorted:
            if f.id == target or f.id == "f_" + target:
                log_fact_delete(session.profile_id, f.id, f.content, "command")
                mgr.facts.delete(f.id)
                return CommandResult(
                    context_for_llm=(
                        f"[命令执行结果 · /fact delete]\n"
                        f"已删除事实：「{f.content}」\n"
                        "请自然地告诉用户这件事已被遗忘。"
                    )
                )

    # 模糊内容匹配
    matches = [f for f in facts_sorted if target in f.content]
    if len(matches) == 1:
        log_fact_delete(session.profile_id, matches[0].id, matches[0].content, "command")
        mgr.facts.delete(matches[0].id)
        return CommandResult(
            context_for_llm=(
                f"[命令执行结果 · /fact delete]\n"
                f"已删除事实：「{matches[0].content}」\n"
                "请自然地告诉用户这件事已被遗忘。"
            )
        )
    if len(matches) > 1:
        lines = [f"{i+1}. {f.content}" for i, f in enumerate(matches)]
        return CommandResult(
            context_for_llm=(
                f"[命令执行结果 · /fact delete]\n"
                f"找到 {len(matches)} 条匹配：\n" + "\n".join(lines) +
                "\n请告诉用户用序号指定要删除哪条，例如 /fact delete 2"
            )
        )

    return CommandResult(
        context_for_llm=(
            f"[命令执行结果 · /fact delete]\n"
            f"未找到含「{target}」的事实。请告知用户。"
        )
    )
