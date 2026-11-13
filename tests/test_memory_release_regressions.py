"""Offline release regressions using temporary stores and synthetic model replies."""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

import test_memory_daily_combined as daily
import test_memory_maintenance_v2 as maintenance
import test_rolling_summary as rolling

from src.api import sessions
from src.core import daily_memory_job as job
from src.memory.conversation_store import ConversationStore
from src.memory.rolling_store import RollingSummaryStore


class AtomicRollingTests(TestCase):
    setUp = rolling.RollingSummaryStoreTests.setUp

    def test_failed_update_preserves_ram_and_disk(self):
        store = RollingSummaryStore(self.root)
        store.update("old", 1)
        before = store.get()
        with patch("src.memory.rolling_store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.update("new", 2)
        self.assertEqual(store.get(), before)
        self.assertEqual(RollingSummaryStore(self.root).get(), before)

    def test_failed_clear_preserves_ram_and_disk(self):
        store = RollingSummaryStore(self.root)
        store.update("old", 1)
        before = store.get()
        with patch("src.memory.rolling_store.os.remove", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                store.clear()
        self.assertEqual(store.get(), before)
        self.assertEqual(RollingSummaryStore(self.root).get(), before)


class RollingReleaseTests(IsolatedAsyncioTestCase):
    setUp = rolling.RollingSummaryManagerTests.setUp
    reply = rolling.RollingSummaryManagerTests.reply
    seed_turns = rolling.RollingSummaryManagerTests.seed_turns
    compact = rolling.RollingSummaryManagerTests.compact

    def prepare_reset(self):
        self.session = SimpleNamespace(id="a", profile_id=rolling.PROFILE,
                                       conversation_store=self.store)
        self.app.state.session_manager = SimpleNamespace(get_by_id=lambda _: self.session)
        self.request = SimpleNamespace(app=self.app)
        self.enterContext(patch("src.api.memory.get_or_create_manager", return_value=self.manager))
        self.enterContext(patch("src.config.profile_loader.load_profile_card", return_value={}))

    async def reset(self):
        result = await sessions.clear_session_history("a", self.request, type="recent")
        return json.loads(result.body) if isinstance(result, JSONResponse) else result

    def prompt_history(self):
        from src.prompt.segments.history import HistorySegment
        return HistorySegment().build(SimpleNamespace(
            extras={"app": self.app}, profile={"memory_config": {"group_chat_merge_into_history": False}},
            n_history_turns=20, user_msg="", store=self.store,
            session=SimpleNamespace(conversation_store=self.store),
        )).messages

    async def test_force_all_sends_every_character_in_bounded_calls(self):
        text = "x" * 7500 + "FINAL IMPORTANT FACT"
        self.manager._cfg["day_summary_max_conv_chars"] = 2000
        self.store.append("user", text)
        self.store.append("assistant", "AFTER LONG MESSAGE")
        with patch("src.memory.memory_manager.get_prompt", side_effect=lambda key:
                   "$conv_text" if key == "memory.rolling_summary_input" else "task"):
            self.assertTrue(await self.compact(force_all=True))
        sent = "".join(call[-1]["content"] for call in self.calls)
        self.assertEqual(sent.count("x"), 7500)
        self.assertTrue(all(len(call[-1]["content"]) <= 2000 for call in self.calls))
        self.assertIn("FINAL IMPORTANT FACT", sent)
        self.assertIn("AFTER LONG MESSAGE", sent)
        self.assertGreater(len(self.calls), 1)
        self.assertEqual(self.manager.rolling.get()["covered_up_to_id"], self.store.memory_tail_id())

    async def test_huge_reset_has_bounded_work_and_keeps_uncovered_context(self):
        self.prepare_reset()
        self.manager._cfg["day_summary_max_conv_chars"] = 2000
        self.store.append("user", "x" * 100_000 + "UNSENT TAIL")
        result = await self.reset()
        self.assertFalse(result["ok"])
        self.assertLessEqual(len(self.calls), 4)
        self.assertEqual(len(self.store.get_recent()), 1)
        self.assertEqual(self.manager.rolling.get()["covered_up_to_id"], 0)

    async def test_disabled_reset_separates_durable_and_prompt_admission(self):
        self.prepare_reset()
        old = self.store.append("user", "PRE RESET SECRET")
        self.manager._cfg["rolling_summary_enabled"] = False
        await self.reset()
        self.assertEqual(self.store.get_recent(), [])
        self.assertEqual(self.store.get_recent_with_ts(), [])
        self.assertEqual(self.prompt_history(), [])
        self.assertEqual(self.store.get_context_start_ts(20), 0)
        self.assertEqual(self.store.memory_messages_after(0, 20)[0]["id"], old)
        self.manager._cfg["rolling_summary_enabled"] = True
        self.seed_turns(20)
        self.assertTrue(await self.compact())
        self.assertNotIn("PRE RESET SECRET", "".join(call[-1]["content"] for call in self.calls))
        self.assertNotIn("PRE RESET SECRET", "".join(m["content"] for m in self.prompt_history()))

    async def test_legacy_timestamp_marker_still_filters_only_prompt_candidates(self):
        self.store.append("user", "OLD MARKER SPEECH")
        cutoff = self.store.get_all()[0].timestamp + 1
        Path(self.store._ctx_path).write_text(json.dumps({"since_ts": cutoff}), encoding="utf-8")
        self.store._load_ctx_ts()
        self.assertFalse(self.store.get_recent())
        self.assertFalse(self.store.get_recent_with_ts())
        self.assertEqual(self.prompt_history(), [])
        self.assertEqual(self.store.get_context_start_ts(20), 0)
        self.assertFalse(await self.compact(force_all=True))
        self.assertEqual(len(self.store.memory_turn_batch_after(0, 20)), 1)
        self.assertEqual(len(self.store.get_all()), 1)

    async def test_regular_long_turn_resumes_without_waiting_for_more_user_quota(self):
        self.manager._cfg["day_summary_max_conv_chars"] = 2000
        self.store.append("user", "x" * 6500 + "LONG TAIL")
        self.seed_turns(14)
        self.assertTrue(await self.compact())
        self.assertLess(self.manager.rolling.get()["covered_up_to_id"], 2)
        for _ in range(10):
            if not await self.compact():
                break
        self.assertIn("LONG TAIL", "".join(call[-1]["content"] for call in self.calls))

    async def test_destructive_mutations_clear_legacy_local_summary(self):
        local = RollingSummaryStore(self.store.storage_root)
        for mutate in (lambda: self.store.delete_by_id(self.store.memory_tail_id()),
                       lambda: self.store.delete_at_index(0),
                       lambda: self.store.delete_from_index(0),
                       lambda: self.store.save_all([]), self.store.clear):
            self.store.append("user", "deleted speech")
            local.update("deleted speech", self.store.memory_tail_id())
            mutate()
            self.assertIsNone(RollingSummaryStore(self.store.storage_root).get())

    async def test_noop_deletions_preserve_summary_and_source_revision(self):
        self.seed_turns(1)
        local = RollingSummaryStore(self.store.storage_root)
        local.update("valid summary", self.store.memory_tail_id())
        before, revision = local.get(), self.store.source_revision
        for mutate in (lambda: self.store.delete_by_id(999),
                       lambda: self.store.delete_at_index(999),
                       lambda: self.store.delete_from_index(999)):
            self.assertFalse(mutate())
            self.assertEqual(local.get(), before)
            self.assertEqual(self.store.source_revision, revision)

    async def test_deletion_during_model_call_cannot_resurrect_summary(self):
        self.seed_turns(3)
        async def stream(_):
            self.store.clear()
            yield "deleted speech"
        self.provider.stream_chat = stream
        self.assertFalse(await self.compact(force_all=True))
        self.assertIsNone(self.manager.rolling.get())

    async def test_cross_connection_replacement_rejects_pending_model_result(self):
        self.seed_turns(3)
        other = ConversationStore(self.store.storage_root, max_turns=0)
        self.addCleanup(other.close)
        async def stream(_):
            messages = other.get_all()
            messages[0].content = "replacement speech"
            other.save_all(messages)
            yield "old speech"
        self.provider.stream_chat = stream
        self.assertFalse(await self.compact(force_all=True))
        self.assertIsNone(self.manager.rolling.get())

    async def test_prompt_rejects_legacy_summary_after_raw_deletion(self):
        from src.prompt.segments.rolling_summary import RollingSummarySegment
        self.seed_turns(3)
        self.manager.rolling.update("DELETED SUMMARY", self.store.memory_tail_id())
        self.store.delete_at_index(0)
        ctx = SimpleNamespace(extras={"app": self.app}, session=SimpleNamespace(
            profile_id=rolling.PROFILE, conversation_store=self.store))
        self.assertFalse(RollingSummarySegment().build(ctx).fired)
        self.assertIsNone(self.manager.rolling.get())

    async def test_partial_failure_can_resume_without_skipping_or_hiding_text(self):
        self.prepare_reset()
        self.manager._cfg["day_summary_max_conv_chars"] = 2000
        self.store.append("user", "x" * 4500 + "LAST SPAN")
        async def stream(messages):
            self.calls.append(messages)
            yield "[LLM Error: offline failure]" if len(self.calls) == 2 else "digest"
        self.provider.stream_chat = stream
        self.assertFalse((await self.reset())["ok"])
        self.assertEqual(self.manager.rolling.get()["covered_up_to_id"], 0)
        self.assertEqual(len(self.store.get_recent()), 1)
        self.reply("digest")
        self.assertTrue((await self.reset())["ok"])
        self.assertIn("LAST SPAN", "".join(call[-1]["content"] for call in self.calls))
        self.assertEqual(self.store.get_recent(), [])
        self.assertEqual(self.store.count_all(), 1)

    async def test_reset_marker_failure_preserves_context_in_ram_and_disk(self):
        self.seed_turns(1)
        with patch("src.memory.conversation_store.os.replace", side_effect=OSError("disk full")), \
             self.assertRaises(OSError):
            self.store.reset_context()
        self.assertEqual(len(self.store.get_recent()), 2)
        reloaded = ConversationStore(self.store.storage_root, max_turns=0)
        self.addCleanup(reloaded.close)
        self.assertEqual(len(reloaded.get_recent()), 2)

    async def test_recent_reset_preserves_speech_arriving_during_compression(self):
        self.prepare_reset()
        self.seed_turns(2)
        async def stream(_):
            self.store.append("user", "concurrent speech")
            yield "old conversation"
        self.provider.stream_chat = stream
        await self.reset()
        self.assertEqual([m["content"] for m in self.store.get_recent()], ["concurrent speech"])
        reloaded = ConversationStore(self.store.storage_root, max_turns=0)
        self.addCleanup(reloaded.close)
        self.assertEqual([m["content"] for m in reloaded.get_recent()], ["concurrent speech"])
        self.assertEqual(self.store.count_all(), 5)

    async def test_recent_reset_model_failure_keeps_context(self):
        self.prepare_reset()
        self.seed_turns(2)
        self.reply("[LLM Error: offline failure]")
        result = await self.reset()
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.store.get_recent()), 4)

    async def test_http_caller_receives_failure_status_when_reset_is_incomplete(self):
        self.prepare_reset()
        self.seed_turns(2)
        self.reply("[LLM Error: offline failure]")
        api = FastAPI()
        api.include_router(sessions.router)
        api.state.session_manager = self.app.state.session_manager
        api.state.config = self.app.state.config
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://fixture") as client:
            response = await client.delete("/sessions/a/history?type=recent")
        self.assertFalse(response.is_success)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(len(self.store.get_recent()), 4)

    async def test_effectively_disabled_reset_preserves_legacy_summary_and_raw_speech(self):
        self.prepare_reset()
        for disabled in ("no_model", "segment_off", "rolling_off", "memory_off"):
            with self.subTest(disabled=disabled):
                self.manager._cfg.update(enabled=disabled != "memory_off",
                                         rolling_summary_enabled=disabled != "rolling_off",
                                         extraction_llm_preset="__none__" if disabled == "no_model" else "memory")
                self.seed_turns(1)
                self.manager.rolling.update("existing summary", 0)
                before = self.manager.rolling.get()
                count = self.store.count_all()
                with patch.object(self.manager, "_rolling_segment_enabled", return_value=disabled != "segment_off"):
                    result = await self.reset()
                self.assertTrue(result["ok"])
                self.assertFalse(result["compacted"])
                self.assertEqual(self.store.get_recent(), [])
                self.assertEqual(self.store.count_all(), count)
                self.assertEqual(self.manager.rolling.get(), before)
        self.get_provider.assert_not_called()

    async def test_recent_reset_disk_failure_keeps_context(self):
        self.prepare_reset()
        self.seed_turns(2)
        with patch("src.memory.rolling_store.os.replace", side_effect=OSError("disk full")):
            result = await self.reset()
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.store.get_recent()), 4)

    async def test_single_reset_leaves_shared_group_context_untouched(self):
        self.prepare_reset()
        self.manager._cfg["rolling_summary_enabled"] = False
        group = ConversationStore(self.root + "/group", max_turns=0)
        self.addCleanup(group.close)
        group.append("user", "shared by A and B")
        self.app.state.group_manager = SimpleNamespace(get_group_ids_for_profile=lambda _: ["g"],
                                                       get_conversation_store=lambda _: group)
        self.seed_turns(1)
        await self.reset()
        self.assertFalse(self.store.get_recent())
        self.assertEqual(len(group.get_recent()), 1)

    async def test_durable_readers_and_evidence_ignore_context_reset(self):
        self.seed_turns(2)
        before = self.store.memory_turn_batch_after(0, 20)
        self.store.reset_context()
        self.assertEqual(self.store.memory_turn_batch_after(0, 20), before)
        self.assertEqual(self.store.memory_messages_after(0, 20), before)
        self.assertTrue(self.store.has_memory_evidence(before[0]))
        self.store.delete_by_id(before[0]["id"])
        self.assertFalse(self.store.has_memory_evidence(before[0]))


class DailyDurableTests(IsolatedAsyncioTestCase):
    setUp = daily.DailyMemoryRequestTests.setUp
    seed = daily.DailyMemoryRequestTests.seed
    reply = daily.DailyMemoryRequestTests.reply
    add = daily.DailyMemoryRequestTests.add
    run_job = daily.DailyMemoryRequestTests.run_job

    async def test_daily_extracts_unconsumed_speech_after_context_reset(self):
        self.seed(old=False)
        self.store.reset_context()
        self.reply({"operations": [self.add()]})
        await self.run_job()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.manager.facts.source_cursors(), {"single": self.assistant_id})

    async def test_portrait_wrapper_distinguishes_due_failure_from_benign_skip(self):
        from src.core import user_portrait
        with patch.object(user_portrait, "_get_recent_conversation_text", return_value="synthetic input"), \
             patch.object(user_portrait, "trigger_portrait_refresh", AsyncMock(return_value=None)) as refresh:
            self.assertIs(await job.run_portrait_refresh_for_profile(daily.PROFILE, self.app), False)
            refresh.assert_awaited_once()
        with patch.object(user_portrait, "_get_recent_conversation_text", return_value=""), \
             patch.object(user_portrait, "trigger_portrait_refresh", AsyncMock()) as refresh:
            self.assertIsNone(await job.run_portrait_refresh_for_profile(daily.PROFILE, self.app))
            refresh.assert_not_awaited()

    async def test_diary_disk_failure_is_failed_without_an_in_memory_completion(self):
        self.manager._cfg["day_summary_enabled"] = True
        self.app.state.config.get_llm_preset = lambda _: self.preset
        self.app.state.config.get_active_llm_preset = lambda: self.preset
        self.reply("Synthetic diary")
        with patch("src.memory.day_store.os.replace", side_effect=OSError("disk full")):
            self.assertFalse(await job.run_day_summary_for_profile(daily.PROFILE, daily.DAY, self.app))
        self.assertFalse(self.manager.day_store.has_summary_for(daily.DAY))

    async def test_diary_read_failure_does_not_generate_a_quiet_day(self):
        self.manager._cfg["day_summary_enabled"] = True
        with patch.object(self.store, "get_all", side_effect=OSError("unreadable history")), \
             patch.object(self.manager, "generate_day_summary", AsyncMock(return_value={"summary": "quiet"})) as generate:
            self.assertFalse(await job.run_day_summary_for_profile(daily.PROFILE, daily.DAY, self.app))
        generate.assert_not_awaited()


class DailyRetryTests(IsolatedAsyncioTestCase):
    setUp = maintenance.MemoryMaintenanceTests.setUp

    async def test_false_result_retries_only_failed_job(self):
        self.summary.side_effect = [False, True]
        self.portrait.return_value = True
        await job.run_daily_for_profile(maintenance.PROFILE, maintenance.YESTERDAY, self.app)
        self.assertIsNone(job._load_last_daily_run(self.root))
        await job.run_daily_for_profile(maintenance.PROFILE, maintenance.YESTERDAY, self.app)
        self.assertEqual(self.summary.await_count, 2)
        self.assertEqual(self.portrait.await_count, 1)
        self.assertEqual(job._load_last_daily_run(self.root), job.date.today().isoformat())

    async def test_exception_does_not_repeat_successful_sibling(self):
        self.summary.return_value = True
        self.portrait.side_effect = [OSError("model unavailable"), True]
        try:
            await job.run_daily_for_profile(maintenance.PROFILE, maintenance.YESTERDAY, self.app)
        except OSError:
            pass
        await job.run_daily_for_profile(maintenance.PROFILE, maintenance.YESTERDAY, self.app)
        self.assertEqual(self.summary.await_count, 1)
        self.assertEqual(self.portrait.await_count, 2)

    async def test_failed_job_has_backoff_and_three_attempt_limit(self):
        self.summary.return_value = False
        self.portrait.return_value = True
        await job.run_daily_for_profile(maintenance.PROFILE, maintenance.YESTERDAY, self.app)
        self.assertFalse(job._daily_retry_due(self.root, datetime.now()))
        self.assertTrue(job._daily_retry_due(self.root, datetime.now() + timedelta(seconds=301)))
        for _ in range(4):
            await job.run_daily_for_profile(maintenance.PROFILE, maintenance.YESTERDAY, self.app)
        self.assertEqual(self.summary.await_count, 3)
        self.assertEqual(self.portrait.await_count, 1)
        self.assertFalse(job._daily_retry_due(self.root, datetime.now() + timedelta(hours=1)))
        self.assertIsNone(job._load_last_daily_run(self.root))

    async def test_scheduler_retries_failed_job_without_restart_or_repeating_siblings(self):
        self.summary.side_effect = [False, True]
        self.portrait.return_value = True
        await job.run_daily_for_profile(maintenance.PROFILE, maintenance.YESTERDAY, self.app)
        with patch.object(job, "_catch_up_missed_runs", AsyncMock()), \
             patch.object(job, "datetime") as clock, \
             patch.object(job.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)):
            clock.now.return_value = datetime.now() + timedelta(seconds=301)
            with self.assertRaises(asyncio.CancelledError):
                await job._run_daily_tasks_loop(self.app)
        self.assertEqual(self.summary.await_count, 2)
        self.assertEqual(self.portrait.await_count, 1)
        self.assertEqual(job._load_last_daily_run(self.root), job.date.today().isoformat())
