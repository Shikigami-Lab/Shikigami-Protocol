"""User sleep window: the hours in which the user is usually asleep, learned from the
times the user actually sends messages.

reflection_user_engagement uses it to tell reflection that the user is most likely
asleep right now (so urgency stays low), and time_context uses it to suppress the
"slow reply" cadence note during those hours. It replaces the previously hard-coded
23:00-06:00 window, which is still the fallback when there is not enough data.

A sleep schedule belongs to the user, not to a profile, so user messages from every
profile chat and every group chat are pooled. A recently created profile has too
little history of its own and would keep falling back to the default.
"""
from __future__ import annotations

import glob
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 30
MIN_OBSERVED_DAYS = 7         # fewer days with messages than this: don't learn, use the default
QUIET_DAY_FRACTION = 0.10     # an hour is quiet if it had messages on <= this share of observed days
MIN_WINDOW_HOURS = 4
MAX_WINDOW_HOURS = 14         # a longer quiet stretch more likely means light use than long sleep
NIGHT_HOURS = range(0, 6)     # a sleep window must include at least one of these hours (00:00-05:59)
CACHE_TTL_SECONDS = 3600
FAILURE_CACHE_TTL_SECONDS = 600   # after a failed scan, serve the default this long before retrying


@dataclass(frozen=True)
class SleepWindow:
    start_hour: int   # inclusive
    end_hour: int     # exclusive; smaller than start_hour when the window wraps past midnight
    learned: bool     # False: not enough data, this is the default window

    def contains(self, hour: int) -> bool:
        if self.start_hour <= self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour

    def label(self) -> str:
        return f"{self.start_hour:02d}:00–{self.end_hour:02d}:00"


DEFAULT_SLEEP_WINDOW = SleepWindow(23, 6, learned=False)


def compute_sleep_window(timestamps: Iterable[float]) -> SleepWindow:
    """Return the longest run of consecutive quiet hours (it may wrap past midnight)
    that includes at least one hour of the night band 00:00-06:00.

    Hours are counted by distinct days rather than by messages: one long late-night
    chat can produce dozens of messages but only shows the user was awake on that day.

    A quiet run entirely in the daytime is never chosen, even when it is longer: for
    someone who messages before and after work, the office hours are quiet too, but
    that is being busy, not asleep. Falls back to DEFAULT_SLEEP_WINDOW with too few
    observed days, when no quiet run touches the night band, or when the chosen run
    is shorter than MIN_WINDOW_HOURS or longer than MAX_WINDOW_HOURS.
    """
    days_by_hour = [set() for _ in range(24)]
    observed_days = set()
    for ts in timestamps:
        dt = datetime.fromtimestamp(ts)
        days_by_hour[dt.hour].add(dt.date())
        observed_days.add(dt.date())

    if len(observed_days) < MIN_OBSERVED_DAYS:
        return DEFAULT_SLEEP_WINDOW

    limit = len(observed_days) * QUIET_DAY_FRACTION
    quiet = [len(days) <= limit for days in days_by_hour]
    if all(quiet) or not any(quiet):
        return DEFAULT_SLEEP_WINDOW

    best_start, best_len = 0, 0
    for start in range(24):
        if not quiet[start] or quiet[(start - 1) % 24]:
            continue  # count each quiet run once, starting from its first hour
        length = 0
        while quiet[(start + length) % 24]:
            length += 1
        touches_night = any((start + i) % 24 in NIGHT_HOURS for i in range(length))
        if touches_night and length > best_len:
            best_start, best_len = start, length

    if not MIN_WINDOW_HOURS <= best_len <= MAX_WINDOW_HOURS:
        return DEFAULT_SLEEP_WINDOW
    return SleepWindow(best_start, (best_start + best_len) % 24, learned=True)


# root argument -> (expires_at, window)
_cache: Dict[Optional[str], Tuple[float, SleepWindow]] = {}


def get_user_sleep_window(root: Optional[str] = None) -> SleepWindow:
    """Learn the sleep window from the last LOOKBACK_DAYS days of user messages across
    all profiles and groups under the project root. Never raises.

    A learned result is cached for CACHE_TTL_SECONDS. If the scan fails, the default
    window is cached for FAILURE_CACHE_TTL_SECONDS so that prompt builds do not rescan
    every chat database on every call.
    """
    now = time.time()
    hit = _cache.get(root)
    if hit is not None and now < hit[0]:
        return hit[1]

    try:
        from src.memory.conversation_store import ConversationStore
        from src.utils.paths import get_project_root

        base = glob.escape(root or get_project_root())
        since = now - LOOKBACK_DAYS * 86400
        timestamps = []
        for kind in ("profiles", "groups"):
            for db_path in glob.glob(os.path.join(base, kind, "*", "chat_records.db")):
                timestamps.extend(
                    ConversationStore.read_user_message_timestamps(os.path.dirname(db_path), since)
                )
        window, ttl = compute_sleep_window(timestamps), CACHE_TTL_SECONDS
    except Exception as e:
        logger.debug("[sleep_window] 学习睡眠时段失败，使用默认时段: %s", e)
        window, ttl = DEFAULT_SLEEP_WINDOW, FAILURE_CACHE_TTL_SECONDS

    _cache[root] = (now + ttl, window)
    return window
