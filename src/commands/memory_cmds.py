"""/recall, /memory status, /help 处理器"""
import logging

from src.commands.base import CommandResult

logger = logging.getLogger(__name__)

_HELP_TEXT = """📌 可用命令（命令结果都会通过 AI 的语言表达）：

/fact list [分类]      — 列出已置顶事实（默认最多 N 条；/fact list all 可看全部）
/fact add [分类:] 内容  — 记住一件关于你的事
/fact delete <序号|词>  — 忘记某件事
/recall <关键词>        — 查找与关键词相关的记忆
/memory status         — 查看记忆系统状态
/topic [trend|回忆|生活] — 让 AI 主动起一个新话题（默认优先新鲜事）
/help                  — 显示本帮助

分类：habit（习惯）/ preference（偏好）/ taboo（禁忌）/ relationship（关系）/ other

示例：
  /fact add 习惯: 每天深夜写代码
  /fact delete 1
  /recall 咖啡"""


def _get_manager(session, app):
    managers = getattr(app.state, "memory_managers", {})
    return managers.get(session.profile_id)


_DEFAULT_RECALL_LIMIT = 10   # 默认召回条数
_VEC_DIST_THRESHOLD   = 0.92  # 向量距离阈值（越大越宽松；cosine distance: 0=完全相同）


async def handle_recall(query: str, session, app) -> CommandResult:
    """
    支持可选数量前缀：/recall [<n>] <关键词>
    示例：
      /recall 用户穿的衣服         → 默认召回 10 条
      /recall 5 用户穿的衣服       → 召回 5 条
    """
    if not query:
        return CommandResult(
            context_for_llm=(
                "[命令执行结果 · /recall]\n"
                "没有提供查询关键词。请告诉用户用法：/recall [条数] <关键词>"
            )
        )

    # 解析可选的数量前缀
    limit = _DEFAULT_RECALL_LIMIT
    parts = query.split(maxsplit=1)
    if len(parts) >= 2 and parts[0].isdigit():
        limit = max(1, min(int(parts[0]), 30))
        query = parts[1].strip()
    if not query:
        return CommandResult(
            context_for_llm="[命令执行结果 · /recall]\n没有提供查询关键词。"
        )

    mgr = _get_manager(session, app)
    if not mgr:
        return CommandResult(context_for_llm="[命令执行结果 · /recall]\n记忆系统尚未初始化。")

    results = []
    seen_contents: set = set()

    # 事实库关键词匹配（精确包含）
    facts = mgr.facts.get_all()
    fact_matches = [f for f in facts if query in f.content]
    for f in fact_matches[:limit]:
        hours = (import_time() - f.updated_at) / 3600
        age = _format_age(hours)
        results.append(f"- {f.content}（{f.category}，{age}前）")
        seen_contents.add(f.content)

    # 向量语义检索（宽松阈值，去重）
    if len(results) < limit and mgr.vectors and mgr.vectors.is_available():
        vec_limit = min(limit - len(results) + 3, 20)
        vec_results = mgr.vectors.search(query, limit=vec_limit)
        for r in vec_results:
            if len(results) >= limit:
                break
            doc = r.get("document", "")
            dist = r.get("distance", 1.0)
            if dist < _VEC_DIST_THRESHOLD and doc not in seen_contents:
                sim_pct = max(0, 100 - int(dist * 100))
                results.append(f"- [语义相关·相似度{sim_pct}%] {doc}")
                seen_contents.add(doc)

    if not results:
        return CommandResult(
            context_for_llm=(
                f"[命令执行结果 · /recall {query}]\n"
                f"关于「{query}」没有找到相关记忆。\n"
                "请自然地告诉用户没有找到，可以稍带遗憾。"
            )
        )

    body = "\n".join(results)
    return CommandResult(
        context_for_llm=(
            f"[命令执行结果 · /recall {query}（共 {len(results)} 条）]\n"
            f"关于「{query}」的相关记忆检索结果如下，请根据这些记忆用自然语言告诉用户你记得什么：\n"
            f"---\n{body}"
        )
    )


async def handle_memory_status(session, app) -> CommandResult:
    mgr = _get_manager(session, app)
    if not mgr:
        return CommandResult(
            context_for_llm=(
                "[命令执行结果 · /memory status]\n"
                "记忆系统尚未初始化。请告知用户。"
            )
        )

    s = mgr.status()
    src = s.get("facts_by_source", {})
    vec_line = (f"向量库：可用，共 {s['vector_count']} 条，模型 {s['vector_provider']}"
                if s["vector_ok"] else "向量库：未启用")
    summary_line = f"对话摘要：已有 {s['summary_count']} 天的回忆"

    return CommandResult(
        context_for_llm=(
            "[命令执行结果 · /memory status]\n"
            "以下是你的记忆系统当前状态，请用角色合适的方式（可以带点俏皮/感慨）告诉用户：\n"
            f"事实库：{s['facts_count']} 条（manual {src.get('manual', 0)} / auto {src.get('auto', 0)} / reflection {src.get('reflection', 0)}）\n"
            f"{vec_line}\n"
            f"{summary_line}"
        )
    )


def handle_help() -> CommandResult:
    return CommandResult(
        direct_response=_HELP_TEXT,
        bypass_llm=True,
    )


def import_time():
    import time
    return time.time()


def _format_age(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)} 分钟"
    if hours < 24:
        return f"{int(hours)} 小时"
    return f"{int(hours / 24)} 天"
