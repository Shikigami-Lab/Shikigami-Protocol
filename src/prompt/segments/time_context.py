"""Time-context segment.

Produces a system block that anchors the AI's sense of time:
  Current temporal anchor  — current datetime + day period
  Cognitive alignment rules — rules telling the AI to trust this anchor
  Time-related context      — frequency stats, last-chat gap, days known,
                              special dates / upcoming holidays, cadence signals

Does NOT include todos/timers (those belong to the active_tasks segment).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from src.config.prompt_loader import get_locale, get_prompt, get_raw, render
from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)


# ── Helpers that read from YAML ───────────────────────────────────────────────

def _get_period(hour: int, locale: str) -> Tuple[str, str]:
    """Return (label, icon) for the given hour from YAML config."""
    periods = get_raw("time_context.periods") or []
    for entry in periods:
        if entry.get("hour_start", 0) <= hour < entry.get("hour_end", 0):
            label = entry.get(locale) or entry.get("zh") or entry.get("en", "")
            return label, entry.get("icon", "")
    default = get_raw("time_context.period_default") or {}
    label = default.get(locale) or default.get("zh") or "深夜"
    return label, default.get("icon", "🌑")


def _get_weekday(weekday_index: int, locale: str) -> str:
    """Return weekday name for given 0-based index (Mon=0)."""
    raw = get_raw("time_context.weekdays") or {}
    names = raw.get(locale) or raw.get("zh") or []
    if isinstance(names, list) and 0 <= weekday_index < len(names):
        return names[weekday_index]
    fallback = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return fallback[weekday_index] if 0 <= weekday_index < 7 else ""


def _get_holidays(locale: str) -> Dict[str, str]:
    """Return MM-DD → label dict from YAML, resolved to locale."""
    raw = get_raw("time_context.holidays") or {}
    result = {}
    for mmdd, entry in raw.items():
        if isinstance(entry, dict):
            result[str(mmdd)] = entry.get(locale) or entry.get("zh") or ""
        else:
            result[str(mmdd)] = str(entry)
    return result


def _get_greeting_hint(tier: str, locale: str) -> Optional[str]:
    raw = get_raw("time_context.greeting_hints") or {}
    entry = raw.get(tier)
    if not entry:
        return None
    if isinstance(entry, dict):
        return entry.get(locale) or entry.get("zh")
    return str(entry)


def _engagement_label(count_7d: int, locale: str) -> str:
    labels = get_raw("time_context.engagement_labels") or []
    for entry in labels:
        if count_7d <= entry.get("max_count", 0):
            return entry.get(locale) or entry.get("zh") or ""
    # last entry (no max_count limit effectively)
    if labels:
        last = labels[-1]
        return last.get(locale) or last.get("zh") or ""
    return ""


def _human_delta(seconds: float, locale: str) -> str:
    minutes = int(seconds / 60)
    if minutes < 2:
        return get_prompt("time_context.human_delta.just_now", locale=locale, default="刚刚")
    if minutes < 60:
        return render("time_context.human_delta.minutes", locale=locale,
                      default="$minutes 分钟前", minutes=minutes)
    hours = minutes // 60
    if hours < 24:
        return render("time_context.human_delta.hours", locale=locale,
                      default="$hours 小时前", hours=hours)
    days = hours // 24
    return render("time_context.human_delta.days", locale=locale,
                  default="$days 天前", days=days)


# ── Built-in Gregorian holidays (fallback if YAML missing) ───────────────────
_BUILTIN_HOLIDAYS_ZH: Dict[str, str] = {
    "01-01": "元旦", "02-14": "情人节", "03-08": "妇女节",
    "04-01": "愚人节", "05-01": "劳动节", "05-04": "青年节",
    "06-01": "儿童节", "08-01": "建军节", "09-10": "教师节",
    "10-01": "国庆节", "10-31": "万圣节", "11-11": "双十一",
    "12-24": "平安夜", "12-25": "圣诞节", "12-31": "跨年夜",
}


def _get_lunar_holidays(now: datetime) -> Dict[str, str]:
    try:
        from zhdate import ZhDate
        y = now.year
        result: Dict[str, str] = {}
        pairs = [
            (y, 1, 1, "春节"), (y, 1, 15, "元宵节"), (y, 5, 5, "端午节"),
            (y, 7, 7, "七夕"), (y, 8, 15, "中秋节"), (y, 9, 9, "重阳节"),
        ]
        for yr, m, d, label in pairs:
            try:
                key = ZhDate(yr, m, d).to_datetime().strftime("%m-%d")
                result[key] = label
            except Exception:
                pass
        return result
    except Exception:
        return {}


def _check_special_dates(now: datetime, profile: Dict[str, Any], locale: str) -> List[str]:
    notices = []
    yaml_holidays = _get_holidays(locale)
    all_holidays = {**_BUILTIN_HOLIDAYS_ZH, **_get_lunar_holidays(now), **yaml_holidays}

    sd = get_raw("time_context.special_date") or {}

    def _label(key: str, fallback: str, **kwargs) -> str:
        entry = sd.get(key, {})
        tmpl = (entry.get(locale) or entry.get("zh") or fallback) if isinstance(entry, dict) else fallback
        from string import Template
        return Template(tmpl).safe_substitute(**kwargs)

    for offset in range(4):
        check = now + timedelta(days=offset)
        key = check.strftime("%m-%d")
        label = all_holidays.get(key)
        if label:
            if offset == 0:
                notices.append(_label("today", "今天是$label 🎉", label=label))
            elif offset == 1:
                notices.append(_label("tomorrow", "明天是$label", label=label))
            else:
                notices.append(_label("in_days", "${offset}天后是$label", offset=offset, label=label))

    for entry in (profile.get("special_dates") or []):
        try:
            date_str = (entry.get("date") or "").strip()
            date_label = entry.get("label", "特殊日期")
            if len(date_str) == 10 and "-" in date_str:
                date_str = date_str[5:]
            elif len(date_str) == 4 and date_str.isdigit():
                date_str = f"{date_str[0:2]}-{date_str[2:4]}"
            for offset in range(4):
                check = now + timedelta(days=offset)
                if check.strftime("%m-%d") == date_str:
                    if offset == 0:
                        notices.append(_label("today_personal", "今天是$label 🌟", label=date_label))
                    elif offset == 1:
                        notices.append(_label("tomorrow_personal", "明天是$label", label=date_label))
                    else:
                        notices.append(_label("in_days_personal", "${offset}天后是$label",
                                               offset=offset, label=date_label))
        except Exception:
            pass

    return notices


def get_time_context_notes(
    now: datetime,
    profile: dict,
    cadence_signals=None,
    session=None,
) -> List[str]:
    locale = get_locale()
    notes = []
    today_str = now.strftime("%Y-%m-%d")
    if session is not None and session.get_segment_last_fired_date("holiday_remind") == today_str:
        pass
    else:
        for notice in _check_special_dates(now, profile, locale):
            notes.append(notice)
        if session is not None and notes:
            session.set_segment_last_fired_date("holiday_remind", today_str)
            session.save_runtime_state()
    if cadence_signals and cadence_signals.has_data:
        if cadence_signals.is_slow and cadence_signals.last_gap_min > 15:
            notes.append(render("time_context.cadence.slow", locale=locale,
                                default="用户已 $minutes 分钟未回复，回复节奏偏慢，可适当提高主动发言意愿。",
                                minutes=f"{cadence_signals.last_gap_min:.0f}"))
        elif cadence_signals.is_quick:
            notes.append(get_prompt("time_context.cadence.quick", locale=locale,
                                    default="用户正在快速交流中，不急于主动打断，优先等待。"))
    return notes


@register
class TimeContextSegment(PromptSegment):
    segment_id  = "time_context"
    priority    = 70
    label       = "时间上下文"
    description = "为 AI 注入详细的时间统计、认知对齐规则和参与度分析"
    is_core     = False

    default_trigger_mode  = "always"
    default_trigger_param = 1.0

    def build(self, ctx: BuildContext) -> SegmentResult:
        locale = get_locale()
        now = datetime.now()
        today = now.date()
        today_start = datetime(now.year, now.month, now.day).timestamp()
        hour = now.hour
        period_label, period_icon = _get_period(hour, locale)
        weekday = _get_weekday(now.weekday(), locale)

        user_name = ctx.extras.get("user_name") or "用户"
        app = ctx.extras.get("app")
        group_manager = getattr(getattr(app, "state", None), "group_manager", None) if app else None
        if app and group_manager is not None:
            from src.memory.merged_history import get_merged_user_activity
            merged = get_merged_user_activity(ctx.session.profile_id, app, user_name)
            last_user_ts = merged.get("last_user_ts")
            count_today  = merged.get("count_today", 0)
            count_24h    = merged.get("count_24h", 0)
            count_7d     = merged.get("count_7d", 0)
            total_count  = merged.get("total_count", 0)
        else:
            last_user_ts = ctx.store.last_user_message_time(user_name)
            count_today  = ctx.store.count_user_messages_since(today_start, user_name)
            count_24h    = ctx.store.count_user_messages_since(time.time() - 86400, user_name)
            count_7d     = ctx.store.count_user_messages_since(time.time() - 7 * 86400, user_name)
            total_count  = ctx.store.total_user_messages_count(user_name)
        first_ts  = ctx.store.first_message_time()
        days_known = int((time.time() - first_ts) / 86400) if first_ts else 0

        try:
            from src.config.effective_config import _load_profile_card
            _rc = _load_profile_card(ctx.session.profile_id).get("reflection_config") or {}
            absence_threshold_h: float = float(_rc.get("long_absence_hours", 48))
        except Exception:
            absence_threshold_h = 48.0

        greeting_tier = "active"
        if last_user_ts is None:
            greeting_tier = "first_of_day"
        else:
            gap_h = (time.time() - last_user_ts) / 3600
            last_dt = datetime.fromtimestamp(last_user_ts)
            if last_dt.date() < today:
                greeting_tier = "first_of_day"
                if gap_h >= absence_threshold_h:
                    greeting_tier = "long_absence"
            elif gap_h >= 1:
                greeting_tier = "short_gap"

        tracker = getattr(getattr(app, "state", None), "cadence_tracker", None) if app else None
        cadence = tracker.get_signals(ctx.session.profile_id) if tracker else None
        if not app:
            logger.debug("[time_context] cadence: no app in extras, skip")
        elif not tracker:
            logger.debug("[time_context] cadence: no cadence_tracker on app.state, skip")
        elif not cadence or not cadence.has_data:
            logger.debug("[time_context] cadence: has_data=False (need >=2 user messages for profile)")
        else:
            logger.debug(
                "[time_context] cadence: has_data=True profile=%s recent_10min=%s avg_gap_min=%.1f "
                "last_gap_min=%.1f is_quick=%s is_slow=%s",
                ctx.session.profile_id,
                getattr(cadence, "recent_count_10min", 0),
                getattr(cadence, "avg_gap_min", 0),
                getattr(cadence, "last_gap_min", 0),
                getattr(cadence, "is_quick", False),
                getattr(cadence, "is_slow", False),
            )

        raw_notices = _check_special_dates(now, ctx.profile, locale)
        today_str = now.strftime("%Y-%m-%d")
        last_remind = ctx.session.get_segment_last_fired_date("holiday_remind")
        special_notices = [] if last_remind == today_str else raw_notices

        sec = get_raw("time_context.sections") or {}

        def _s(key: str, fallback: str, **kw) -> str:
            entry = sec.get(key, {})
            tmpl = (entry.get(locale) or entry.get("zh") or fallback) if isinstance(entry, dict) else fallback
            if kw:
                from string import Template
                return Template(tmpl).safe_substitute(**kw)
            return tmpl

        lines: List[str] = []

        # Section 1: 时空背景锚点
        lines.append(_s("anchor_header", "【当前时空背景锚点】"))
        lines.append(_s("datetime_line", "现在是 $date（$weekday），$time  时段：$icon $period",
                        date=now.strftime("%Y年%m月%d日") if locale == "zh" else now.strftime("%Y-%m-%d"),
                        weekday=weekday,
                        time=now.strftime("%H:%M"),
                        icon=period_icon,
                        period=period_label))

        if special_notices:
            for notice in special_notices:
                lines.append(f"· {notice}")
            notice_suffix_entry = (get_raw("time_context.special_date") or {}).get("notice_suffix", {})
            if isinstance(notice_suffix_entry, dict):
                suffix = notice_suffix_entry.get(locale) or notice_suffix_entry.get("zh") or ""
            else:
                suffix = str(notice_suffix_entry)
            if suffix:
                lines.append(f"→ {suffix}" if not suffix.startswith("→") else suffix)
            ctx.session.set_segment_last_fired_date("holiday_remind", today_str)
            ctx.session.save_runtime_state()

        # Section 2: 认知对齐规则
        lines.append("")
        lines.append(_s("cognitive_header", "【认知对齐规则】"))
        rules_entry = sec.get("cognitive_rules", {})
        rules = (rules_entry.get(locale) or rules_entry.get("zh") or []) if isinstance(rules_entry, dict) else []
        for rule in rules:
            from string import Template
            lines.append(Template(str(rule)).safe_substitute(period=period_label))

        hint = _get_greeting_hint(greeting_tier, locale)
        if hint:
            lines.append(f"- 💡 {hint}")

        # Section 3: 时间相关上下文
        lines.append("")
        lines.append(_s("context_header", "【时间相关上下文】"))
        engagement_prefix = _s("engagement_prefix", "参与度: ")
        lines.append(engagement_prefix + _engagement_label(count_7d, locale))
        lines.append(_s("frequency_line",
                        "最近聊天频率: 今天$today次，最近24h共$h24次，最近7天共$d7次。",
                        today=count_today, h24=count_24h, d7=count_7d))
        lines.append(_s("total_count_line", "总聊天次数: $total次。", total=total_count))

        if last_user_ts is not None:
            lines.append(_s("last_chat_line", "上次聊天: $delta。",
                            delta=_human_delta(time.time() - last_user_ts, locale)))
        else:
            lines.append(_s("no_chat_yet", "上次聊天: 尚未有过对话记录。"))

        lines.append(_s("days_known_line", "认识时间: 已经相识$days天了。", days=max(1, days_known)))

        if cadence and cadence.has_data:
            if cadence.is_quick:
                lines.append(_s("cadence_quick",
                                "对话节奏：对方回复很频繁（近10分钟$count条），处于快速交流模式，回复可以更简洁活跃。",
                                count=cadence.recent_count_10min))
                logger.debug("[time_context] cadence: appended quick")
            elif cadence.is_slow:
                lines.append(_s("cadence_slow",
                                "对话节奏：对方回复节奏较慢（平均约$avg_min分钟一条），保持耐心，不催促，语气可以更从容。",
                                avg_min=cadence.avg_gap_min))
                logger.debug("[time_context] cadence: appended slow")

        content = "\n".join(lines)
        return SegmentResult(messages=[{"role": "system", "content": content}])
