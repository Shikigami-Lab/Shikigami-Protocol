import asyncio
from datetime import datetime
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from src.core import daily_memory_job as job
from src.memory.long_term_store import LongTermStore


class MemoryReviewScheduleTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())
        self.session = SimpleNamespace(profile_id="fixture", storage_root=self.root)
        self.app = SimpleNamespace(state=SimpleNamespace(
            session_manager=SimpleNamespace(list_sessions=lambda: [self.session]),
        ))
        self.manager = self.open_manager()
        self.enterContext(patch.object(job, "_get_manager_for_session", side_effect=lambda *_: self.manager))
        self.service = SimpleNamespace(run_daily=AsyncMock())
        self.enterContext(patch.object(job, "curation_for", return_value=self.service))
        self.preset = {"model": "fixture"}
        self.enterContext(patch.object(job, "curation_preset", return_value=self.preset))
        self.prompt = self.enterContext(patch("src.memory.curation.fixed_review_prompt", create=True,
                                             return_value="Scheduled review prompt"))

    def open_manager(self, **config):
        return SimpleNamespace(
            facts=LongTermStore(self.root), vectors=None, _extraction_lock=asyncio.Lock(),
            _cfg={"enabled": True, "daily_curation_enabled": True, **config},
        )

    def write_state(self, **changes):
        revision, cursors, facts, state = self.manager.facts.curation_snapshot()
        self.assertTrue(self.manager.facts.apply_curation(revision, facts, cursors, {**state, **changes}))

    def marker(self):
        return LongTermStore(self.root).curation_snapshot()[-1].get("last_scheduled_check_date")

    async def check(self, *time):
        await job._run_scheduled_curation(self.session, self.app, now=datetime(*time))

    async def test_first_check_waits_until_inherited_time_and_records_no_work_across_reload(self):
        self.manager._cfg.update(daily_run_at_hour=9, daily_run_at_minute=25)
        await self.check(2026, 11, 7, 9, 24)
        self.service.run_daily.assert_not_awaited()
        self.assertIsNone(self.marker())
        await self.check(2026, 11, 7, 9, 25)
        self.service.run_daily.assert_awaited_once()
        self.assertEqual(self.service.run_daily.call_args.args[0], "2026-11-07")
        self.assertEqual(self.marker(), "2026-11-07")
        self.assertNotIn("daily", LongTermStore(self.root).curation_snapshot()[-1])
        self.manager = self.open_manager(daily_run_at_hour=9, daily_run_at_minute=25)
        await self.check(2026, 11, 7, 23, 59)
        self.service.run_daily.assert_awaited_once()

    async def test_legacy_attempt_day_and_partial_time_override_anchor_interval(self):
        self.manager._cfg.update(
            curation_interval_days=3, curation_run_at_hour=None, curation_run_at_minute=40,
            daily_run_at_hour=9, daily_run_at_minute=25,
        )
        self.write_state(daily={"day": "2026-11-04", "status": "failed", "requests": 1})
        await self.check(2026, 11, 6, 23, 59)
        await self.check(2026, 11, 7, 9, 39)
        self.service.run_daily.assert_not_awaited()
        await self.check(2026, 11, 7, 9, 40)
        self.service.run_daily.assert_awaited_once()
        self.assertEqual(self.marker(), "2026-11-07")

    async def test_overdue_check_runs_once_and_anchors_to_actual_day(self):
        self.manager._cfg.update(curation_interval_days=4, curation_run_at_hour=21, curation_run_at_minute=10)
        self.write_state(last_scheduled_check_date="2026-10-23")
        await self.check(2026, 11, 7, 10, 0)
        await self.check(2026, 11, 7, 21, 10)
        await self.check(2026, 11, 11, 21, 9)
        self.service.run_daily.assert_awaited_once()
        self.assertEqual(self.marker(), "2026-11-07")
        await self.check(2026, 11, 11, 21, 10)
        self.assertEqual([call.args[0] for call in self.service.run_daily.await_args_list],
                         ["2026-11-07", "2026-11-11"])

    async def test_disabled_and_explicit_no_model_skip_without_claiming(self):
        for key, value in (("enabled", False), ("daily_curation_enabled", False),
                           ("extraction_llm_preset", "__none__")):
            self.manager = self.open_manager(**{key: value})
            await self.check(2026, 11, 7, 12, 0)
        self.service.run_daily.assert_not_awaited()
        self.assertIsNone(self.marker())

    async def test_reload_during_review_cannot_start_a_second_check(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def run(*args, **kwargs):
            self.write_state(daily={"day": "2026-11-07", "status": "running", "requests": 1})
            entered.set()
            await release.wait()

        self.service.run_daily.side_effect = run
        first = asyncio.create_task(self.check(2026, 11, 7, 12, 0))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            self.assertIsNone(self.marker())
            self.manager = self.open_manager()
            await self.check(2026, 11, 7, 12, 1)
            self.service.run_daily.assert_awaited_once()
        finally:
            release.set()
            await first

    async def test_cancel_before_work_leaves_schedule_due(self):
        self.manager._cfg["curation_interval_days"] = 30
        entered = asyncio.Event()

        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.service.run_daily.side_effect = wait
        task = asyncio.create_task(self.check(2026, 11, 7, 12, 0))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.marker())
        self.manager = self.open_manager(curation_interval_days=30)
        self.service.run_daily.side_effect = None
        await self.check(2026, 11, 7, 12, 1)
        self.assertEqual(self.marker(), "2026-11-07")
        self.assertEqual(self.service.run_daily.await_count, 2)

    async def test_same_prompt_is_used_for_the_batch_request_and_limit_default(self):
        request = AsyncMock(return_value=[{"action": "keep"}])

        async def run(day, decide, **kwargs):
            self.assertEqual(kwargs["review_limit"], 500)
            await decide(["fixture-row"])

        self.service.run_daily.side_effect = run
        with patch.object(job, "request_batch_review", request):
            await self.check(2026, 11, 7, 12, 0)
        self.assertEqual(self.prompt.call_args_list, [((self.app,), {}), ((self.app,), {})])
        request.assert_awaited_once_with(["fixture-row"], self.preset, review_prompt="Scheduled review prompt")


class MemoryReviewLoopTests(IsolatedAsyncioTestCase):
    async def test_shared_warmup_launches_independent_daily_and_review_loops(self):
        reviewing, diary, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def review(app):
            reviewing.set()
            await release.wait()

        async def daily(app):
            await reviewing.wait()
            diary.set()

        real_sleep = asyncio.sleep

        async def wait(seconds):
            await real_sleep(0)

        with patch.object(job, "_run_curation_schedule_loop", review, create=True), \
             patch.object(job, "_run_daily_tasks_loop", daily, create=True), \
             patch.object(job, "_catch_up_missed_runs", AsyncMock()), \
             patch.object(job.asyncio, "sleep", AsyncMock(side_effect=wait)) as sleep:
            task = asyncio.create_task(job.run_daily_memory_loop(SimpleNamespace(state=SimpleNamespace())))
            try:
                await asyncio.wait_for(diary.wait(), timeout=1)
                self.assertFalse(task.done())
                sleep.assert_awaited_once_with(90)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_review_loop_can_check_another_profile_while_one_is_busy(self):
        a, b = [SimpleNamespace(profile_id=id) for id in ("a", "b")]
        sessions = [a]
        app = SimpleNamespace(state=SimpleNamespace(session_manager=SimpleNamespace(list_sessions=lambda: sessions)))
        entered, second, stop, cancelled = [asyncio.Event() for _ in range(4)]

        async def review(session, app):
            if session is b:
                second.set()
                return
            entered.set()
            try:
                await stop.wait()
            finally:
                cancelled.set()

        ticks = 0

        async def sleep(seconds):
            nonlocal ticks
            self.assertGreater(seconds, 0)
            self.assertLessEqual(seconds, 60)
            ticks += 1
            if ticks == 1:
                await entered.wait()
                sessions.append(b)
            else:
                await stop.wait()

        with patch.object(job, "_run_scheduled_curation", review, create=True), patch.object(job.asyncio, "sleep", sleep):
            loop = getattr(job, "_run_curation_schedule_loop", None)
            self.assertIsNotNone(loop)
            task = asyncio.create_task(loop(app))
            try:
                await asyncio.wait_for(second.wait(), timeout=1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
