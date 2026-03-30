"""reflection_user_engagement — User activity stats for reflection.

Priority 52, inject_into="reflection".

Unconditionally injects conversation frequency statistics and current time
into the reflection prompt so the AI can make informed urgency decisions
based on the user's actual activity patterns — not just silence duration.
"""
import time
from datetime import datetime

from src.prompt.base import PromptSegment, ReflectionBuildContext, SegmentResult
from src.prompt.registry import register
from src.config.prompt_loader import get_locale


def _get_period_zh(hour: int) -> str:
    if 5 <= hour < 9:   return "清晨"
    if 9 <= hour < 12:  return "上午"
    if 12 <= hour < 14: return "中午"
    if 14 <= hour < 18: return "下午"
    if 18 <= hour < 22: return "晚上"
    return "深夜"


def _get_period_en(hour: int) -> str:
    if 5 <= hour < 9:   return "dawn"
    if 9 <= hour < 12:  return "morning"
    if 12 <= hour < 14: return "midday"
    if 14 <= hour < 18: return "afternoon"
    if 18 <= hour < 22: return "evening"
    return "late night"


@register
class ReflectionUserEngagementSegment(PromptSegment):
    segment_id = "reflection_user_engagement"
    inject_into = "reflection"
    is_readonly = True
    is_core = False
    default_enabled = True
    priority = 52
    label = "用户规律感知"
    description = "学习活跃时间段，在合适时机才开口"

    def build(self, ctx) -> SegmentResult:
        if not isinstance(ctx, ReflectionBuildContext):
            return SegmentResult(messages=[])

        store = ctx.store
        locale = ctx.locale or get_locale()
        now = time.time()
        dt_now = datetime.fromtimestamp(now)
        hour = dt_now.hour

        if locale == "en":
            period_label = _get_period_en(hour)
            time_str = dt_now.strftime("%H:%M")
            header = "[Time & Engagement]"
        else:
            period_label = _get_period_zh(hour)
            time_str = dt_now.strftime("%H:%M")
            header = "【时间与参与度】"

        lines = [header]

        # Current time
        if locale == "en":
            lines.append(f"Now {time_str} ({period_label}).")
        else:
            lines.append(f"现在 {time_str}，时段：{period_label}。")

        # Conversation frequency stats
        if store is not None:
            try:
                user_name = ctx.user_name or "用户"
                today_start = datetime(dt_now.year, dt_now.month, dt_now.day).timestamp()
                count_today = store.count_user_messages_since(today_start, user_name)
                count_24h   = store.count_user_messages_since(now - 86400, user_name)
                count_7d    = store.count_user_messages_since(now - 7 * 86400, user_name)

                # 3-day trend: yesterday, day-2, day-3
                day1 = store.count_user_messages_since(now - 86400, user_name)
                day2 = store.count_user_messages_since(now - 2 * 86400, user_name) - day1
                day3 = store.count_user_messages_since(now - 3 * 86400, user_name) - day1 - day2

                first_ts = store.first_message_time()
                known_days = max(1, int((now - first_ts) / 86400)) if first_ts else 1
                avg_per_day = round(count_7d / min(7, known_days), 1) if known_days > 0 else 0

                if locale == "en":
                    lines.append(
                        f"Today: {count_today} msgs | Last 24h: {count_24h} | Last 7d: {count_7d} "
                        f"(avg {avg_per_day}/day)."
                    )
                    # Trend hint
                    if day1 > 0 and day2 > 0 and day1 < day2 * 0.6:
                        lines.append("Engagement trend: declining over past 3 days → lower urgency threshold.")
                    elif count_today == 0 and count_24h == 0:
                        lines.append("User hasn't messaged today — may be busy or away.")
                else:
                    lines.append(
                        f"今天：{count_today} 条 | 近24h：{count_24h} 条 | 近7天：{count_7d} 条"
                        f"（日均 {avg_per_day} 条）。"
                    )
                    if day1 > 0 and day2 > 0 and day1 < day2 * 0.6:
                        lines.append("参与度趋势下行（近3天回复减少）→ 适当降低 urgency，不要频繁打扰。")
                    elif count_today == 0 and count_24h == 0:
                        lines.append("用户今天尚未发消息，可能在忙或不在。")
            except Exception:
                pass

        # Active hour hint from silent_seconds + current time
        silent = ctx.silent_seconds or 0
        if locale == "en":
            if hour >= 23 or hour < 6:
                lines.append("Current time is late night/early dawn — user may be asleep; keep urgency low.")
            elif silent > 14400:
                lines.append(f"User has been silent {int(silent/3600)}h+ — if no strong reason, don't interrupt repeatedly.")
        else:
            if hour >= 23 or hour < 6:
                lines.append("当前属于深夜/清晨时段，用户可能已入睡 → urgency 保持低位。")
            elif silent > 14400:
                lines.append(f"用户已沉默 {int(silent/3600)} 小时以上，如无强烈动机不要反复打扰。")

        content = "\n".join(lines)
        return SegmentResult(messages=[{"role": "system", "content": content}])
