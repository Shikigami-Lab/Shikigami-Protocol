"""Timer commands — /timer [label] <N><unit> | list | stop [label]"""
import logging
import re
from datetime import datetime
from typing import Optional, Tuple

from src.commands.base import CommandResult, register_command
from src.utils.debug_logger import log_timer, log_error

logger = logging.getLogger(__name__)

# ── 时间单位映射 ───────────────────────────────────────────────────────────────
_UNIT_MAP = {
    "秒": 1, "s": 1,
    "分钟": 60, "分": 60, "m": 60,
    "小时": 3600, "时": 3600, "h": 3600,
}
_UNIT_DISPLAY = {
    1: "秒", 60: "分钟", 3600: "小时",
}
# 匹配时间片段（数字+单位），支持浮点
_TIME_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(秒|分钟|分|小时|时|[smh])(?!\w)')


def _parse_timer_cmd(args: str) -> Tuple[str, Optional[int], Optional[str]]:
    """Returns (action, seconds, label).
    action: 'start' | 'list' | 'stop' | None (parse error)
    """
    args = args.strip()

    # 子命令：list / stop
    if args in ("list", "查看", "状态", "ls", ""):
        return "list", None, None
    if args.startswith(("stop", "停止", "取消", "cancel")):
        label = re.sub(r'^(stop|停止|取消|cancel)\s*', '', args).strip() or None
        return "stop", None, label

    # 在 args 中找时间片段
    m = _TIME_RE.search(args)
    if not m:
        return None, None, None

    unit_str = m.group(2)
    seconds = int(float(m.group(1)) * _UNIT_MAP[unit_str])
    label = args[:m.start()].strip() or None
    return "start", seconds, label


def _fmt_duration(seconds: int) -> str:
    if seconds >= 3600:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}小时{m}分钟" if m else f"{h}小时"
    if seconds >= 60:
        m = seconds // 60
        s = seconds % 60
        return f"{m}分{s}秒" if s else f"{m}分钟"
    return f"{seconds}秒"


async def handle_timer(sub: str, args: str, session, app) -> CommandResult:
    from src.tools.timer.manager import start_timer, stop_timer, list_timers, get_timer_status

    # Reconstruct full args from sub + args (dispatch splits on first space)
    full_args = (sub + (" " + args if args else "")).strip()
    action, seconds, label = _parse_timer_cmd(full_args)

    if action == "list" or action is None and not full_args:
        timers = list_timers()
        if not timers:
            return CommandResult(
                context_for_llm="[计时器] 当前没有运行中的计时器。请自然地告知用户。"
            )
        lines = []
        import time as _time
        for t in timers:
            remaining = max(0, int(t["end_timestamp"] - _time.time()))
            lines.append(f"  「{t['label']}」剩余 {_fmt_duration(remaining)}")
        return CommandResult(
            context_for_llm=(
                f"[计时器列表]\n" + "\n".join(lines) +
                "\n请自然地向用户报告各计时器剩余时间。"
            )
        )

    if action == "stop":
        timers = list_timers()
        if not timers:
            return CommandResult(
                context_for_llm="[计时器] 当前没有运行中的计时器。请告知用户。"
            )
        target = None
        if label:
            for t in timers:
                if label.lower() in t.get("label", "").lower():
                    target = t
                    break
        if not target:
            target = timers[-1]
        stop_timer(target["id"])
        log_timer(session.id, "stop", timer_id=target["id"], label=target["label"])
        return CommandResult(
            context_for_llm=(
                f"[计时器·已停止]\n"
                f"计时器「{target['label']}」已停止。\n"
                f"请自然地确认，可加一句温暖的话。"
            )
        )

    if action == "start" and seconds is not None:
        display_label = label or "计时器"
        timer_id = start_timer(seconds, display_label)
        log_timer(session.id, "start", timer_id=timer_id, label=display_label, seconds=seconds)
        now_str = datetime.now().strftime("%H:%M")
        duration_str = _fmt_duration(seconds)

        if label:
            ctx = (
                f"[计时器·已启动]\n"
                f"计时器「{display_label}」已启动，时长 {duration_str}（现在 {now_str}），id:{timer_id}\n"
                f"用户说：{label}\n"
                f"请先自然确认计时器已启动，然后回应用户说的内容。"
            )
        else:
            ctx = (
                f"[计时器·已启动]\n"
                f"计时器已启动，时长 {duration_str}（现在 {now_str}），id:{timer_id}\n"
                f"请用温暖自然的语气向用户确认，一句话内完成。"
            )
        return CommandResult(context_for_llm=ctx)

    return CommandResult(
        context_for_llm=(
            "[计时器] 无法解析计时指令。\n"
            "用法：/timer [标签] <N>秒|分钟|小时，/timer list，/timer stop [标签]\n"
            "请告知用户正确用法。"
        )
    )


# 模块加载时自动注册
register_command("timer", handle_timer)
