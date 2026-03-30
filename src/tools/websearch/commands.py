"""WebSearch commands — /search <query>"""
import logging

from src.commands.base import CommandResult, register_command

logger = logging.getLogger(__name__)


async def handle_search(sub: str, args: str, session, app) -> CommandResult:
    # base.py 在只有一个参数时 args == sub，需去重；多个单词时 args 是 sub 之后的部分
    query = sub if (not args or args == sub) else f"{sub} {args}".strip()
    if not query:
        return CommandResult(
            context_for_llm="[搜索] 请提供搜索关键词，例如：/search 今天天气"
        )

    from src.tools.websearch.searcher import search, format_for_llm
    results = await search(query, max_results=5)
    ctx = format_for_llm(results, query)
    return CommandResult(
        context_for_llm=(
            f"{ctx}\n\n"
            "请根据以上搜索结果，用你自己的语气和风格回答用户，"
            "不要逐条照搬，选取最相关的内容自然融入回复。"
        )
    )


register_command("search", handle_search)
