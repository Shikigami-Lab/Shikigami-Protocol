"""Durable one-shot reminders and their single server-lifespan scheduler."""
import asyncio
from contextlib import asynccontextmanager, closing, suppress
from datetime import datetime, timedelta, timezone
import logging
import re
import sqlite3
import time
import uuid

logger = logging.getLogger(__name__)

DUE_AT_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(\.[0-9]{1,6})?([Zz]|[+-]([01][0-9]|2[0-3]):[0-5][0-9])?$"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECOND = timedelta(microseconds=1)


def _due_time(due_at):
    match = re.fullmatch(DUE_AT_PATTERN, due_at) if isinstance(due_at, str) else None
    if match is None:
        raise ValueError("due_at must be an ISO date and time, optionally with a UTC offset")
    fraction = (match[1] or ".").ljust(7, "0")
    offset = "+00:00" if (match[2] or "").upper() == "Z" else (match[2] or "")
    try:
        due = datetime.fromisoformat(due_at[:19].upper() + fraction + offset)
        if offset:
            return due_at, (due - _EPOCH) // _MICROSECOND
        # Resolve default local times on the host for the target date. A fold
        # needs an explicit offset and a skipped time must be corrected.
        candidates = {due.replace(fold=fold).timestamp() for fold in (0, 1)}
        valid = [ts for ts in candidates if datetime.fromtimestamp(ts) == due]
        if len(valid) == 1:
            resolved = datetime.fromtimestamp(valid[0]).astimezone()
            return resolved.isoformat(), (resolved - _EPOCH) // _MICROSECOND
    except (ValueError, OSError, OverflowError):
        raise ValueError("due_at is not a valid date and time") from None
    raise ValueError("Local reminder time is ambiguous or does not exist; confirm the time and offset")


def _wall_clock_us():
    return time.time_ns() // 1000


def _timer(row):
    result = dict(row)
    result["end_timestamp"] = result.pop("due_us") / 1_000_000
    result["session_id"] = result.pop("profile_id")
    result["kind"] = "reminder"
    return result


class ReminderStore:
    def __init__(self, path, *, clock=_wall_clock_us):
        self.path = path
        self._clock = clock
        self._wake = None
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, label TEXT NOT NULL,
                due_at TEXT NOT NULL, due_us INTEGER NOT NULL, status TEXT NOT NULL
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS reminders_pending
                ON reminders(due_us, id) WHERE status = 'scheduled'""")
            db.execute("""CREATE INDEX IF NOT EXISTS reminders_profile
                ON reminders(profile_id, due_us, id)""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=1)
        db.row_factory = sqlite3.Row
        return db

    def _notify_scheduler(self):
        wake = self._wake
        if wake is not None:
            loop, event = wake
            loop.call_soon_threadsafe(event.set)

    def schedule(self, profile_id, due_at, label):
        due_at, due_us = _due_time(due_at)
        if not isinstance(label, str) or not label.strip() or len(label) > 128:
            raise ValueError("Reminder label must contain 1 to 128 characters")
        row = dict(id=str(uuid.uuid4()), profile_id=profile_id, label=label.strip(),
                   due_at=due_at, due_us=due_us, status="scheduled")
        with closing(self._connect()) as db, db:
            if due_us <= self._clock():
                raise ValueError("Reminder time must be in the future")
            db.execute("""INSERT INTO reminders(id, profile_id, label, due_at, due_us, status)
                VALUES (:id, :profile_id, :label, :due_at, :due_us, :status)""", row)
        self._notify_scheduler()
        return _timer(row)

    def list_for(self, profile_id):
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM reminders WHERE profile_id=? ORDER BY due_us, id", (profile_id,)
            )
            return [_timer(row) for row in rows]

    def cancel(self, profile_id, timer_id):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT label FROM reminders WHERE id=? AND profile_id=?",
                             (timer_id, profile_id)).fetchone()
            if row is not None:
                db.execute("DELETE FROM reminders WHERE id=?", (timer_id,))
        if row is not None:
            self._notify_scheduler()
            return dict(row)
        return None

    def remove_profile(self, profile_id):
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM reminders WHERE profile_id=?", (profile_id,))
        self._notify_scheduler()

    @asynccontextmanager
    async def running(self, publish):
        if self._wake is not None:
            raise RuntimeError("Reminder scheduler is already running")
        with closing(self._connect()) as db, db:
            db.execute("UPDATE reminders SET status='missed' WHERE status='scheduled' AND due_us<=?",
                       (self._clock(),))
        wake = asyncio.Event()
        self._wake = (asyncio.get_running_loop(), wake)
        task = asyncio.create_task(self._run(wake, publish))
        try:
            yield
        finally:
            self._wake = None
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run(self, wake, publish):
        while True:
            wake.clear()
            try:
                with closing(self._connect()) as db:
                    next_due = db.execute("""SELECT due_us FROM reminders
                        WHERE status='scheduled' ORDER BY due_us, id LIMIT 1""").fetchone()
                    delay = (next_due["due_us"] - self._clock()) / 1_000_000 if next_due else 30
                    if delay <= 0:
                        # Commit before the SSE attempt. If the process exits between
                        # the two, the row stays due in the panel without replay.
                        with db:
                            db.execute("BEGIN IMMEDIATE")
                            row = db.execute("""SELECT * FROM reminders WHERE status='scheduled' AND due_us<=?
                                ORDER BY due_us, id LIMIT 1""", (self._clock(),)).fetchone()
                            if row is not None:
                                db.execute("UPDATE reminders SET status='due' WHERE id=?", (row["id"],))
                if delay <= 0:
                    if row is not None:
                        await publish({**_timer(row), "status": "due"})
                    await asyncio.sleep(0)
                    continue
            except Exception:
                logger.exception("Dated reminder scheduler iteration failed")
                delay = 1

            # Recheck wall time periodically for clock changes and system sleep.
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), min(delay, 30))
