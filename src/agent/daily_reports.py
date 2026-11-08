"""SQLite scheduling for daily reports. Supply an ignored path like data/daily_reports.db."""

import asyncio
from contextlib import asynccontextmanager, closing
from datetime import date, datetime, time, timedelta
import logging
from pathlib import Path
import re
import sqlite3
from time import time as wall_clock
from uuid import uuid4

logger = logging.getLogger(__name__)

_DAY = timedelta(days=1)
_GRACE_SECONDS = 90
_CALLBACK_TIMEOUT = 90
_POLL_SECONDS = 5


def _slot(day, hhmm):
    # Resolve each date in the host zone. fold=0 chooses the first occurrence
    # on a fall-back day and shifts forward by the gap on a spring-forward day.
    return datetime.combine(day, time.fromisoformat(hhmm)).timestamp()


def _latest_due(row, today, now):
    for day in (today, today - _DAY):
        if day.isoformat() >= row["next_day"]:
            slot = _slot(day, row["time"])
            if slot <= now:
                return day, slot
    return None


def _configuration(row):
    next_run_at = _slot(date.fromisoformat(row["next_day"]), row["time"]) if row["next_day"] else None
    return {
        "time": row["time"],
        "instructions": row["instructions"],
        "enabled": bool(row["enabled"]),
        "next_run_at": next_run_at,
        "last_run_at": row["last_run_at"],
        "last_status": row["last_status"],
    }


class DailyReportStore:
    """Persist the next local date and latest slot outcome for each profile."""

    def __init__(self, path, *, clock=wall_clock):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._scheduler = None
        self._callbacks = set()
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS daily_reports (
                profile_id TEXT PRIMARY KEY NOT NULL,
                time TEXT NOT NULL,
                instructions TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                next_day TEXT,
                last_day TEXT,
                last_run_at REAL,
                last_status TEXT,
                claim_id TEXT,
                claim_deadline REAL
            )""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=0.2)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _get(db, profile_id):
        return db.execute("SELECT * FROM daily_reports WHERE profile_id=?", (profile_id,)).fetchone()

    def get(self, profile_id):
        with closing(self._connect()) as db:
            row = self._get(db, profile_id)
        return _configuration(row) if row is not None else None

    def configure(self, profile_id, time, instructions, enabled):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get(db, profile_id)
            if row is not None:
                time = row["time"] if time is None else time
                instructions = row["instructions"] if instructions is None else instructions
                enabled = bool(row["enabled"]) if enabled is None else enabled
            elif enabled is None:
                enabled = True
            return self._configure(db, profile_id, time, instructions, enabled, row)

    def _configure(self, db, profile_id, time, instructions, enabled, row):
        if not isinstance(time, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", time):
            raise ValueError("Report time must be host-local HH:MM")
        if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 1000:
            raise ValueError("Report instructions must contain 1 to 1000 characters")
        if not isinstance(enabled, bool):
            raise ValueError("Report enabled must be a boolean")

        if row is not None and row["time"] == time and row["enabled"] == enabled:
            if row["instructions"] == instructions:
                return _configuration(row)
            next_day = row["next_day"]
        elif enabled:
            now = self._clock()
            day = datetime.fromtimestamp(now).date()
            if _slot(day, time) < now:
                day += _DAY
            if row is not None and row["last_day"] is not None:
                day = max(day, date.fromisoformat(row["last_day"]) + _DAY)
            next_day = day.isoformat()
        else:
            next_day = None

        db.execute("""INSERT INTO daily_reports(profile_id, time, instructions, enabled, next_day)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                time=excluded.time, instructions=excluded.instructions, enabled=excluded.enabled,
                next_day=excluded.next_day, claim_id=NULL, claim_deadline=NULL,
                last_status=CASE WHEN last_status='running' THEN 'skipped' ELSE last_status END
            """, (profile_id, time, instructions, enabled, next_day))
        return _configuration(self._get(db, profile_id))

    def remove_profile(self, profile_id):
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM daily_reports WHERE profile_id=?", (profile_id,))

    def _claim_due(self, started_at):
        claims = []
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = self._clock()
            today = datetime.fromtimestamp(now).date()
            # Another scheduler may own a live claim. Expire it only when its
            # window has ended or the clock has rolled back before its start.
            db.execute("""UPDATE daily_reports SET last_status='failed',
                claim_id=NULL, claim_deadline=NULL
                WHERE claim_id IS NOT NULL AND (claim_deadline<=? OR last_run_at>?)""", (now, now))
            rows = db.execute("""SELECT * FROM daily_reports
                WHERE enabled=1 AND next_day<=? AND claim_id IS NULL""", (today.isoformat(),)).fetchall()
            for row in rows:
                due = _latest_due(row, today, now)
                if due is None:
                    continue
                day, slot = due
                profile_id = row["profile_id"]
                next_day = (day + _DAY).isoformat()
                if slot < started_at or day != today or now - slot > _GRACE_SECONDS or datetime.fromtimestamp(slot).date() != day:
                    db.execute("""UPDATE daily_reports
                        SET next_day=?, last_day=?, last_run_at=?, last_status='missed' WHERE profile_id=?""",
                        (next_day, day.isoformat(), slot, profile_id))
                    continue

                claim_id = uuid4().hex
                db.execute("""UPDATE daily_reports
                    SET next_day=?, last_day=?, last_run_at=?, last_status='running',
                        claim_id=?, claim_deadline=? WHERE profile_id=?""",
                    (next_day, day.isoformat(), now, claim_id, now + _CALLBACK_TIMEOUT, profile_id))
                claims.append((profile_id, claim_id, row["instructions"]))
        return claims

    def _finish(self, profile_id, claim_id, status):
        with closing(self._connect()) as db, db:
            db.execute("""UPDATE daily_reports SET last_status=?, claim_id=NULL, claim_deadline=NULL
                WHERE profile_id=? AND claim_id=?""", (status, profile_id, claim_id))

    async def _deliver(self, profile_id, claim_id, instructions, callback, shutdown):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CALLBACK_TIMEOUT
        active = True
        status = "failed"
        work = None

        def still_current():
            if not active or shutdown.done() or loop.time() >= deadline:
                return False
            try:
                with closing(self._connect()) as db:
                    row = self._get(db, profile_id)
                now = self._clock()
                return (row is not None and row["claim_id"] == claim_id
                        and datetime.fromtimestamp(now).date().isoformat() == row["last_day"]
                        and row["last_run_at"] <= now < row["claim_deadline"]
                        and loop.time() < deadline)
            except sqlite3.Error:
                logger.exception("Daily report claim check failed")
                return False

        async def invoke():
            try:
                result = await callback(profile_id, instructions, still_current)
                return result if not shutdown.done() and loop.time() < deadline else "failed"
            except Exception as exc:
                logger.warning("Daily report callback failed (%s)", type(exc).__name__)
                return "failed"

        try:
            if shutdown.done():
                return
            work = asyncio.create_task(invoke())
            self._callbacks.add(work)
            work.add_done_callback(self._callbacks.discard)
            done, _ = await asyncio.wait((work, shutdown), timeout=_CALLBACK_TIMEOUT,
                                         return_when=asyncio.FIRST_COMPLETED)
            if work in done and not work.cancelled():
                result = work.result()
                if result in ("completed", "skipped"):
                    status = result
        finally:
            # Revoke publication before delivering cancellation, even if the
            # callback catches it. The claim remains consumed after shutdown.
            active = False
            if work is not None and not work.done():
                work.cancel()
            try:
                self._finish(profile_id, claim_id, status)
            except sqlite3.Error:
                logger.exception("Daily report outcome could not be saved; claim will expire")

    @asynccontextmanager
    async def running(self, callback):
        """Call async callback(profile_id, instructions, still_current) for due claims.

        Check still_current immediately before publishing and return "completed"
        or "skipped". Callbacks should cooperate with asyncio cancellation.
        """
        if self._scheduler is not None and not self._scheduler.done():
            raise RuntimeError("Daily report scheduler is already running")
        shutdown = asyncio.get_running_loop().create_future()
        self._scheduler = asyncio.create_task(self._run(callback, shutdown, self._clock()))
        try:
            yield
        finally:
            if not shutdown.done():
                shutdown.set_result(None)
            await asyncio.shield(self._scheduler)

    async def _run(self, callback, shutdown, started_at):
        pending = set()
        try:
            while not shutdown.done():
                pending = {task for task in pending if not task.done()}
                try:
                    claims = self._claim_due(started_at)
                except sqlite3.Error:
                    logger.exception("Daily report scheduler tick failed")
                else:
                    # Claim the entire tick before starting any model work.
                    for profile_id, claim_id, instructions in claims:
                        pending.add(asyncio.create_task(
                            self._deliver(profile_id, claim_id, instructions, callback, shutdown)))
                await asyncio.wait((shutdown,), timeout=_POLL_SECONDS)
        finally:
            if not shutdown.done():
                shutdown.set_result(None)
            await asyncio.gather(*pending, return_exceptions=True)
