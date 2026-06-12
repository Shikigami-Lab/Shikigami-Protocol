"""NL触发器框架 — 自然语言意图触发工具执行

触发器无需 / 前缀，基于正则+关键词意图检测，
结果同样通过 AI 的嘴说出来（CommandResult.context_for_llm）。

注册的触发器：
  - TimerNLTrigger
  - WebSearchNLTrigger
"""
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from src.commands.base import CommandResult

logger = logging.getLogger(__name__)


class NLTrigger(ABC):
    @abstractmethod
    def detect(self, msg: str) -> bool: ...

    @abstractmethod
    async def execute(self, msg: str, session, app) -> Optional[CommandResult]: ...


_REGISTRY: List[NLTrigger] = []


def register_nl_trigger(trigger: NLTrigger):
    _REGISTRY.append(trigger)


def get_nl_triggers() -> List[NLTrigger]:
    return list(_REGISTRY)


# ── TimerNLTrigger ────────────────────────────────────────────────────────────

class TimerNLTrigger(NLTrigger):
    """计时器自然语言触发器，复用 src.core.timer 的意图检测逻辑。"""

    def detect(self, msg: str) -> bool:
        from src.tools.timer.manager import (
            detect_timer_start_intent,
            detect_timer_stop_intent,
            detect_timer_query_intent,
        )
        return (detect_timer_start_intent(msg)
                or detect_timer_stop_intent(msg)
                or detect_timer_query_intent(msg))

    async def execute(self, msg: str, session, app) -> Optional[CommandResult]:
        from src.tools.timer.manager import (
            detect_timer_start_intent,
            detect_timer_stop_intent,
            detect_timer_query_intent,
            extract_timer_label_and_duration,
            extract_timer_label_only,
            start_timer,
            stop_timer,
            list_timers,
            get_timer_status,
        )

        if detect_timer_start_intent(msg):
            return await self._handle_start(msg, extract_timer_label_and_duration, start_timer)

        if detect_timer_stop_intent(msg):
            return await self._handle_stop(msg, extract_timer_label_only, stop_timer, list_timers)

        if detect_timer_query_intent(msg):
            return await self._handle_query(msg, extract_timer_label_only, list_timers, get_timer_status)

        return None

    async def _handle_start(self, msg, extract_fn, start_fn) -> CommandResult:
        seconds, label = extract_fn(msg)
        if seconds is None:
            return CommandResult(
                context_for_llm=(
                    "[计时器]\n"
                    "用户想启动计时器，但未指定时长。请提示用法，例如：帮我计时20分钟"
                )
            )
        timer_id = start_fn(seconds, label or "计时器")
        minutes = seconds // 60
        remainder = seconds % 60
        duration_str = (f"{minutes}分{remainder}秒" if remainder else f"{minutes}分钟") if minutes else f"{seconds}秒"
        current_time = datetime.now().strftime("%H:%M")
        return CommandResult(
            context_for_llm=(
                f"[计时器已启动]\n"
                f"计时器「{label or '计时器'}」已启动，时长 {duration_str}（当前 {current_time}），id:{timer_id}\n"
                "请用温暖自然的语气向用户确认计时器已启动，一句话内完成，可带轻微情感。"
            )
        )

    async def _handle_stop(self, msg, extract_label_fn, stop_fn, list_fn) -> CommandResult:
        timers = list_fn()
        if not timers:
            return CommandResult(
                context_for_llm=(
                    "[计时器]\n"
                    "当前没有运行中的计时器。请自然地告知用户。"
                )
            )
        user_label = extract_label_fn(msg)
        target = None
        if user_label:
            for t in timers:
                if t.get("label") == user_label:
                    target = t
                    break
        if not target:
            target = timers[-1]

        info = stop_fn(target["id"])
        display = target.get("label", "计时器")
        if info:
            elapsed = int(time.time() - (target["end_timestamp"] - target["seconds"]))
            return CommandResult(
                context_for_llm=(
                    f"[计时器已停止]\n"
                    f"计时器「{display}」已停止，已运行约 {elapsed} 秒。\n"
                    "请自然地告知用户计时器已停止，可加一句温暖的话。"
                )
            )
        return CommandResult(
            context_for_llm=(
                f"[计时器]\n"
                f"停止计时器「{display}」失败。请告知用户。"
            )
        )

    async def _handle_query(self, msg, extract_label_fn, list_fn, status_fn) -> CommandResult:
        timers = list_fn()
        if not timers:
            return CommandResult(
                context_for_llm=(
                    "[计时器]\n"
                    "当前没有运行中的计时器。请自然地告知用户。"
                )
            )
        user_label = extract_label_fn(msg)
        target = None
        if user_label:
            for t in timers:
                if t.get("label") == user_label:
                    target = t
                    break
        if not target:
            target = timers[-1]

        status = status_fn(target["id"])
        display = target.get("label", "计时器")
        if status:
            remaining = status["remaining_seconds"]
            mins = remaining // 60
            secs = remaining % 60
            remaining_str = f"{mins}分{secs}秒" if mins else f"{secs}秒"
            return CommandResult(
                context_for_llm=(
                    f"[计时器状态]\n"
                    f"计时器「{display}」剩余 {remaining_str}。\n"
                    "请简短自然地告诉用户剩余时间。"
                )
            )
        return CommandResult(
            context_for_llm=(
                f"[计时器]\n"
                f"无法获取计时器「{display}」的状态。请告知用户。"
            )
        )


# ── WebSearchNLTrigger ────────────────────────────────────────────────────────

import re as _re

_SEARCH_PATTERNS = _re.compile(
    r"(帮我搜|帮我查|搜索一下|搜一下|查一下|查查|网上查|网上搜|"
    r"搜搜看|查查看|找一下|网上找|上网查|上网搜|百度一下|google一下)",
    _re.IGNORECASE,
)


class WebSearchNLTrigger(NLTrigger):
    """网络搜索自然语言触发器。

    触发条件：用户明确表达搜索意图（帮我搜/查一下/...）。
    不响应普通问句，避免每个问题都触发搜索。
    """

    def detect(self, msg: str) -> bool:
        return bool(_SEARCH_PATTERNS.search(msg))

    async def execute(self, msg: str, session, app) -> Optional[CommandResult]:
        # 提取搜索关键词：去掉触发词后剩余部分
        query = _SEARCH_PATTERNS.sub("", msg).strip("，。！？ ")
        if not query:
            return CommandResult(
                context_for_llm="[搜索] 用户想搜索但未指定关键词，请询问要搜什么。"
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


# ── TopicNLTrigger ────────────────────────────────────────────────────────────

_TOPIC_PATTERNS = _re.compile(
    r"(换个话题|聊点别的|聊点新的|聊点新鲜的|说点别的|说点新鲜的|"
    r"有什么新鲜事|有啥新鲜事|讲点新鲜事|"
    r"change the topic|talk about something (else|new)|anything new)",
    _re.IGNORECASE,
)
# 去掉触发词后允许残留的语气词/称呼填充（"那我们换个话题吧" → 残留 "那我们"）
_TOPIC_FILLER = _re.compile(
    r"(?i)(let'?s|so|then|okay|ok|hey|well|now|那我们|那就|我们|咱们|那|就|来|你|"
    r"好不好|行不行|好吗|可以吗|怎么样|呗)"
    r"|[吧呗呀啊吗呢哦喔嘛诶哈~～!！?？。，,.\s]+"
)


class TopicNLTrigger(NLTrigger):
    """找话题自然语言触发器：复用 /topic 的零 LLM 调用选材逻辑。

    仅在「裸请求」时触发：去掉触发词与语气词后消息基本为空。
    "换个话题，聊聊你昨天说的那个" 这类用户已点题的消息不被劫持。
    """

    def detect(self, msg: str) -> bool:
        m = msg.strip()
        if len(m) > 36 or not _TOPIC_PATTERNS.search(m):
            return False
        rest = _TOPIC_FILLER.sub("", _TOPIC_PATTERNS.sub("", m))
        return len(rest) <= 2

    async def execute(self, msg: str, session, app) -> Optional[CommandResult]:
        from src.commands.topic_cmds import pick_topic_material, _format_context
        picked = pick_topic_material(session, app)
        if picked is None:
            # 无素材时不注入，让 AI 按本性自行转换话题
            return None
        return CommandResult(context_for_llm=_format_context(picked, msg.strip()))


# ── 注册默认触发器 ─────────────────────────────────────────────────────────────
register_nl_trigger(TimerNLTrigger())
register_nl_trigger(WebSearchNLTrigger())
register_nl_trigger(TopicNLTrigger())
