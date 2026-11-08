import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from src.agent import daily_reports
from src.agent.daily_reports import DailyReportStore


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 8, 8, 59)

    def __call__(self):
        return self.now.timestamp()

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), 2)


class DailyReportTests(IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        polling = patch.object(daily_reports, "_POLL_SECONDS", 0.005)
        polling.start()
        self.addCleanup(polling.stop)

    def schedule(self, case="fixture"):
        clock = Clock()
        path = self.root / case / "data" / "daily_reports.db"
        return DailyReportStore(path, clock=clock), path, clock

    def test_round_trip_validation_and_removal(self):
        store, path, clock = self.schedule()
        assert store.get("role") is None
        expected = {
            "time": "09:00", "instructions": "Summarize the day.", "enabled": True,
            "next_run_at": (clock.now + timedelta(minutes=1)).timestamp(),
            "last_run_at": None, "last_status": None,
        }
        assert store.configure("role", "09:00", expected["instructions"], True) == expected
        reopened = DailyReportStore(path, clock=clock)
        assert reopened.get("role") == expected
        for invalid in ("9:00", "24:00", "09:60", "09:00 "):
            with self.assertRaises(ValueError):
                store.configure("role", invalid, "Report", True)
        for invalid in ("", " \n", "x" * 1001):
            with self.assertRaises(ValueError):
                store.configure("role", "09:00", invalid, True)
        with self.assertRaises(ValueError):
            store.configure("new-role", None, "Report", True)
        assert store.configure("role", "09:00", "x" * 1000, False)["next_run_at"] is None
        assert store.configure("role", None, "New instructions", None)["enabled"] is False
        store.remove_profile("role")
        store.remove_profile("role")
        assert reopened.get("role") is None

    def test_configuring_after_slot_waits_for_tomorrow(self):
        store, _, clock = self.schedule()
        clock.advance(seconds=61)
        config = store.configure("role", "09:00", "Report", True)
        assert config["next_run_at"] == (clock.now + timedelta(days=1, seconds=-1)).timestamp()
        assert config["last_status"] is None

    async def test_concurrent_schedulers_and_restart_only_claim_once(self):
        store, path, clock = self.schedule()
        store.configure("role", "09:00", "Report", True)
        other = DailyReportStore(path, clock=clock)
        clock.advance(minutes=1)
        calls = []
        release = asyncio.Event()

        async def callback(profile_id, instructions, still_current):
            assert other.get(profile_id)["last_status"] == "running"
            assert still_current()
            calls.append((profile_id, instructions))
            await release.wait()
            return "completed"

        async with store.running(callback), other.running(callback):
            await until(lambda: calls)
            release.set()
            await until(lambda: store.get("role")["last_status"] == "completed")
        reopened = DailyReportStore(path, clock=clock)
        assert reopened.get("role")["last_run_at"] == clock()
        async with reopened.running(callback):
            await asyncio.sleep(0.03)
            assert calls == [("role", "Report")]
            clock.advance(days=1)
            await until(lambda: len(calls) == 2)
            await until(lambda: reopened.get("role")["last_status"] == "completed")

    async def test_missed_offline_slots_advance_to_next_day(self):
        store, path, clock = self.schedule()
        store.configure("role", "09:00", "Report", True)
        clock.advance(days=12, minutes=5)
        reopened = DailyReportStore(path, clock=clock)
        calls = []

        async def callback(*args):
            calls.append(args)
            return "completed"

        async with reopened.running(callback):
            await until(lambda: reopened.get("role")["last_status"] == "missed")
            row = reopened.get("role")
            assert row["last_run_at"] == clock.now.replace(minute=0).timestamp()
            assert row["next_run_at"] == (clock.now.replace(minute=0) + timedelta(days=1)).timestamp()
            assert calls == []
            clock.advance(days=1, minutes=-4)
            await until(lambda: reopened.get("role")["last_status"] == "completed")
            assert len(calls) == 1

    async def test_offline_slots_are_missed_even_within_previous_grace_window(self):
        store, _, clock = self.schedule()
        clock.now = clock.now.replace(hour=23, minute=58)
        store.configure("within", "23:59", "Report", True)
        store.configure("outside", "23:58", "Report", True)
        clock.advance(minutes=2, seconds=30)
        calls = []

        async def callback(profile_id, instructions, still_current):
            calls.append(profile_id)
            return "completed"

        async with store.running(callback):
            await until(lambda: store.get("within")["last_status"] == "missed")
            assert store.get("outside")["last_status"] == "missed"
            assert store.get("within")["next_run_at"] == clock.now.replace(
                hour=23, minute=59, second=0).timestamp()
            assert calls == []

    async def test_clock_rollback_and_time_edit_keep_claimed_day(self):
        store, _, clock = self.schedule()
        store.configure("role", "09:00", "Report", True)
        clock.advance(minutes=1)
        calls = []

        async def callback(profile_id, instructions, still_current):
            calls.append(profile_id)
            return "completed"

        async with store.running(callback):
            await until(lambda: store.get("role")["last_status"] == "completed")
            clock.advance(hours=-1)
            updated = store.configure("role", "09:30", "Edited", True)
            assert updated["next_run_at"] == (clock.now + timedelta(days=1, hours=1, minutes=30)).timestamp()
            clock.advance(hours=1, minutes=30)
            await asyncio.sleep(0.02)
            assert calls == ["role"]

    async def test_same_tick_roles_are_claimed_before_any_callback_and_run_concurrently(self):
        store, _, clock = self.schedule()
        for profile in ("slow", "fast"):
            store.configure(profile, "09:00", "Report", True)
        clock.advance(minutes=1)
        observed = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def callback(profile_id, instructions, still_current):
            if not observed:
                observed.extend(store.get(profile)["last_status"] for profile in ("slow", "fast"))
            if profile_id == "slow":
                started.set()
                await release.wait()
            else:
                await started.wait()
            return "completed"

        async with store.running(callback):
            await until(lambda: store.get("fast")["last_status"] == "completed")
            assert observed == ["running", "running"]
            assert store.get("slow")["last_status"] == "running"
            release.set()
            await until(lambda: store.get("slow")["last_status"] == "completed")

    async def test_stale_callback_cannot_publish_or_overwrite_configuration(self):
        for change in ("edit", "disable_and_reenable", "remove_and_recreate"):
            with self.subTest(change=change):
                await self._stale_callback_cannot_publish_or_overwrite_configuration(change)

    async def _stale_callback_cannot_publish_or_overwrite_configuration(self, change):
        store, path, clock = self.schedule(change)
        store.configure("role", "09:00", "Original", True)
        clock.advance(minutes=1)
        guards = []
        released = asyncio.Event()
        finished = asyncio.Event()

        async def callback(profile_id, instructions, still_current):
            guards.append(still_current)
            await released.wait()
            assert not still_current()
            finished.set()
            return "completed"

        async with store.running(callback):
            await until(lambda: guards)
            other = DailyReportStore(path, clock=clock)
            assert guards[0]()
            if change == "disable_and_reenable":
                other.configure("role", "10:00", "Original", False)
                assert other.get("role")["next_run_at"] is None
            if change == "remove_and_recreate":
                other.remove_profile("role")
            updated = other.configure("role", "10:00", "Edited", True)
            assert not guards[0]()
            expected_day = clock.now + timedelta(days=change != "remove_and_recreate", hours=1)
            assert updated["next_run_at"] == expected_day.timestamp()
            assert updated["last_status"] == (None if change == "remove_and_recreate" else "skipped")
            released.set()
            await asyncio.wait_for(finished.wait(), 2)
            await asyncio.sleep(0.02)
            assert other.get("role") == updated

    async def test_terminal_outcomes_do_not_retry_and_do_not_block_other_roles(self):
        with patch.object(daily_reports, "_CALLBACK_TIMEOUT", 0.05):
            for outcome in ("skipped", "error", "timeout", "invalid"):
                with self.subTest(outcome=outcome):
                    await self._check_terminal_outcome(outcome)

    async def _check_terminal_outcome(self, outcome):
        store, _, clock = self.schedule(outcome)
        store.configure("a", "09:00", "Report", True)
        store.configure("b", "09:00", "Report", True)
        clock.advance(minutes=1)
        calls = []
        cancelled_guard = []

        async def callback(profile_id, instructions, still_current):
            calls.append(profile_id)
            if profile_id == "b":
                return "completed"
            if outcome == "error":
                raise RuntimeError("synthetic error")
            if outcome == "timeout":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled_guard.append(still_current())
                    return "completed"
            return outcome

        expected = "skipped" if outcome == "skipped" else "failed"
        async with store.running(callback):
            await until(lambda: store.get("b")["last_status"] == "completed")
            await until(lambda: store.get("a")["last_status"] == expected)
            await asyncio.sleep(0.03)
            assert sorted(calls) == ["a", "b"]
            assert cancelled_guard == ([False] if outcome == "timeout" else [])

    async def test_cancelled_scheduler_leaves_terminal_claim(self):
        store, path, clock = self.schedule()
        store.configure("role", "09:00", "Report", True)
        clock.advance(minutes=1)
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def callback(profile_id, instructions, still_current):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                assert not still_current()
                stopped.set()

        async def lifespan():
            async with store.running(callback):
                await asyncio.Event().wait()

        task = asyncio.create_task(lifespan())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(stopped.wait(), 2)
        assert DailyReportStore(path, clock=clock).get("role")["last_status"] == "failed"

    async def test_abandoned_claim_expires_without_replay(self):
        for seconds in (91, -1):
            with self.subTest(clock_change=seconds):
                await self._check_abandoned_claim(seconds)

    async def _check_abandoned_claim(self, seconds):
        store, path, clock = self.schedule(str(seconds))
        store.configure("role", "09:00", "Report", True)
        clock.advance(minutes=1)
        assert len(store._claim_due(clock())) == 1  # Simulate process death immediately after commit.
        clock.advance(seconds=seconds)

        reopened = DailyReportStore(path, clock=clock)
        calls = []

        async def callback(*args):
            calls.append(args)
            return "completed"

        async with reopened.running(callback):
            await until(lambda: reopened.get("role")["last_status"] == "failed")
            assert calls == []
