"""Temporal evidence for memory writers; storage/update times are not event dates."""
from datetime import datetime
import math
import re

_FLOATING_DATE = re.compile(
    r'(?:上|下|这|本)(?:一?个)?(?:星期|礼拜|周)(?:[一二三四五六日天1-7末])?'
    r'|(?:上|下|这|本)(?:个)?月|(?:明|今|去|后|前)年|(?:明|昨|今|后|前)天|(?:今|明|昨)晚'
    r'|\b(?:today|tomorrow|yesterday|tonight)\b'
    r'|\b(?:next|last|this)\s+(?:week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',
    re.IGNORECASE,
)


def has_floating_date(text: str) -> bool:
    return bool(_FLOATING_DATE.search(text or ''))


def source_time(value) -> str:
    if type(value) not in (int, float) or not math.isfinite(value):
        return 'unknown'
    try:
        return datetime.fromtimestamp(value).astimezone().isoformat(timespec='seconds')
    except (OverflowError, OSError, ValueError):
        return 'unknown'


def write_time_context(turns, locale='zh') -> str:
    dates = '\n'.join(f'{i}: {source_time(turn.get("timestamp"))}' for i, turn in enumerate(turns, 1))
    if locale == 'en':
        rules = (
            '[Source message times; same line order as the conversation]\n'
            'Resolve relative event dates against the originating message time, not the refresh date. '
            'Write explicit YYYY-MM-DD dates when the evidence is unambiguous; retain plan vs completed status. '
            'Do not persist floating dates such as next Tuesday or tomorrow. If the date or reference is '
            'unknown/ambiguous, state that uncertainty without inventing a date. Existing memory update '
            'times do not establish when its quoted relative date was spoken. Do not rebase old memories on today.\n'
        )
    else:
        rules = (
            '【原消息时间索引：与对话行一一对应】\n'
            '相对日期必须以原话的时间为参照，不以整理时间计算。证据明确时，把事件日期写为YYYY-MM-DD，'
            '保留计划/已发生的区别；不得将“下个礼拜二”“明天”等悬空日期写入新记忆。'
            '原消息时间unknown或语义有歧义时，明确日期尚未确定，不猜测。旧记忆的更新时间不等于'
            '原话时间或事件日期，不能把旧记忆里的相对日期按今天重算。\n'
        )
    return rules + dates


def read_time_notice(locale='zh') -> str:
    if locale == 'en':
        return ('Memory/portrait update times are maintenance metadata, not event dates. Use explicit event '
                'dates in the text. Unanchored relative dates in older memories are uncertain; consult original '
                'dated messages when needed instead of interpreting them relative to today. A past scheduled '
                'date alone does not prove the event actually happened. For unconfirmed plans, ask whether they '
                'went ahead instead of asking how they went. Do not presuppose attendance or completion.')
    return ('记忆/画像更新时间仅表示维护时间，不代表事件发生或原话说出的时间。事件以正文明确日期为准；'
            '旧记录中未绑定原话日期的相对时间不能按今天重算，必要时查带日期的原始对话确认。计划日期已过也不代表事情已经发生。'
            '对于尚无完成证据的计划，只能询问后来是否成行；不要用“面试顺利吗”等预设已经参加或完成的问题。')
