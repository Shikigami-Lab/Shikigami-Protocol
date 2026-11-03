"""Synthetic, offline tests of task provenance through memory consumers."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock
import asyncio

from src.memory.conversation_store import ConversationStore, Message
from src.memory.merged_history import get_merged_messages_for_date, get_merged_recent_messages
from src.memory import memory_manager as memory
from src.prompt.segments.history import HistorySegment
from src.llm.message_normalization import normalize_messages


DAY = "2026-01-15"
TS = datetime(2026, 1, 15, 12).timestamp()


class MemoryTaskProvenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-memory-provenance-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        # Suppress production debug file writes, including function-local imports.
        import src.utils.debug_logger as debug
        for module in (debug, memory):
            for name in vars(module):
                if name.startswith("log_") and callable(getattr(module, name)):
                    self.guards.enter_context(patch.object(module, name))
        self.guards.enter_context(patch("socket.create_connection", side_effect=AssertionError("offline test")))
        self.store = self.make_store("single")
        self.group = self.make_store("group")
        self.session = SimpleNamespace(profile_id="synthetic", conversation_store=self.store)
        self.app = SimpleNamespace(state=SimpleNamespace(
            config=SimpleNamespace(user_name="Human", get_active_llm_preset=lambda: {"model": "synthetic"}),
            group_manager=SimpleNamespace(
                get_group_ids_for_profile=lambda pid: ["g"],
                get_group=lambda gid: {"display_name": "Synthetic group"},
                get_conversation_store=lambda gid: self.group)))
        self.manager = memory.MemoryManager("synthetic", str(self.root / "memory"),
            profile_memory_cfg={"enabled": True, "extraction_frequency": 1, "day_summary_enabled": True})
        self.guards.enter_context(patch.object(self.manager, "_build_day_summary_context", return_value={
            "character_name": "Fixture", "persona_brief": "Synthetic persona", "user_name": "Human",
            "recent_summaries": "", "affinity_status": "", "activity_note": ""}))
        templates = {"memory.extraction_default": "Extract from:\n$conv_text",
                     "memory.day_summary": "Summarize:\n$conv_text",
                     "memory.day_summary_quiet": "Quiet synthetic day"}
        self.guards.enter_context(patch.object(memory, "get_prompt", side_effect=lambda key: templates.get(key, "")))
        self.guards.enter_context(patch("src.config.prompt_loader.get_locale", return_value="en"))
        self.guards.enter_context(patch("src.config.prompt_loader.render", return_value="Synthetic persona"))
        self.calls = []
        self.response = "Synthetic summary"

        async def stream(messages):
            self.calls.append(deepcopy(messages))
            yield self.response

        self.provider = self.guards.enter_context(patch("src.llm.registry.get_provider",
            return_value=SimpleNamespace(stream_chat=stream)))

    def make_store(self, name):
        store = ConversationStore(str(self.root / name), max_turns=0)
        self.addCleanup(store.close)
        return store

    def populate(self):
        self.store.save_all([
            Message("user", "USER_KEPT", TS, "Human"),
            Message("assistant", "COMPLETE_KEPT", TS + 1, "Fixture"),
            Message("assistant", "CANCELLED_DROP", TS + 2, "Fixture", "run-c", "cancelled"),
            Message("assistant", "FAILED_DROP", TS + 3, "Fixture", "run-f", "failed"),
        ])
        self.group.save_all([
            Message("user", "GROUP_USER_KEPT", TS + 4, "Human"),
            Message("assistant", "GROUP_COMPLETE_KEPT", TS + 5, "Other"),
            Message("assistant", "GROUP_FAILED_DROP", TS + 6, "Other", "run-g", "failed"),
        ])

    def assert_clean_prompt(self, expected):
        prompt = "\n".join(m["content"] for m in self.calls[-1])
        self.assertNotIn("_DROP", prompt)
        for text in expected:
            self.assertIn(text, prompt)

    async def test_recent_and_daily_mergers_preserve_single_and_group_provenance(self):
        self.populate()
        original = self.store.get_all_with_id()
        for profile in ({}, {"memory_config": {"group_chat_merge_into_history": False}}):
            recent = get_merged_recent_messages(self.session, self.app, profile, 20)
            daily = get_merged_messages_for_date(self.session, self.app, profile, DAY)
            for rows in (recent, daily):
                for source, store in (("single", self.store), ("group", self.group)):
                    originals = {m["id"]: m for m in store.get_all_with_id()}
                    for row in (m for m in rows if m["source"] == source):
                        prior = originals[row["id"]]
                        for field in ("agent_run_id", "generation_status", "content"):
                            self.assertEqual(row[field], prior[field])
            self.assertEqual(len(recent), 7 if not profile else 4)
            self.assertEqual(len(daily), len(recent))
        self.assertEqual(self.store.get_all_with_id(), original)

    async def test_merged_group_partial_gets_trusted_prompt_marker(self):
        self.populate()
        ctx = SimpleNamespace(extras={"app": self.app}, profile={}, session=self.session,
                              store=self.store, n_history_turns=20, user_msg="current")
        wire = normalize_messages(HistorySegment().build(ctx).messages)
        self.assertIn("generation_status=failed", wire[0]["content"])
        group_entry = wire[0]["content"].splitlines()[-2]
        self.assertIn("generation_status=failed", group_entry)
        self.assertTrue(any("GROUP_FAILED_DROP" in m["content"] for m in wire[1:]))

    async def test_extraction_filters_partials_and_preserves_normal_fact_writes(self):
        self.populate()
        rows = get_merged_recent_messages(self.session, self.app, {}, 20)
        original = deepcopy(rows)
        self.response = json.dumps([{"content": "Synthetic user likes mathematical puzzles",
                                     "category": "preference", "weight": 1.5}])
        for custom in ("", "Custom extraction {conversation}", "Custom extraction without placeholder"):
            self.manager._cfg["extraction_prompt"] = custom
            await self.manager.auto_extract(rows, {}, self.app)
            self.assert_clean_prompt(["USER_KEPT", "COMPLETE_KEPT", "GROUP_USER_KEPT", "GROUP_COMPLETE_KEPT"])
        self.assertEqual(self.manager.facts.get_all()[0].content, "Synthetic user likes mathematical puzzles")
        self.assertEqual(rows, original)

    async def test_summary_filters_object_and_dict_inputs_before_counting(self):
        self.populate()
        for merged in (False, True):
            rows = get_merged_messages_for_date(self.session, self.app, {}, DAY) if merged else None
            for custom in ("", "Custom summary {conversation}"):
                self.manager._cfg["day_summary_prompt"] = custom
                result = await self.manager.generate_day_summary(DAY, self.store, {}, self.app,
                    force=True, day_msgs_override=rows)
                self.assertEqual(result["message_count"], 4 if merged else 2)
                expected = ["USER_KEPT", "COMPLETE_KEPT"]
                if merged:
                    expected += ["GROUP_USER_KEPT", "GROUP_COMPLETE_KEPT"]
                self.assert_clean_prompt(expected)
        self.assertEqual(self.store.count_all(), 4)

    async def test_preview_and_range_match_completed_summary_input(self):
        self.populate()
        for merged, expected_count in ((False, 2), (True, 4)):
            options = dict(session=self.session, app=self.app, profile={}) if merged else {}
            preview = self.manager.get_summary_range_preview(self.store, DAY, 1, **options)
            self.assertEqual(preview[0]["message_count"], expected_count)
            result = await self.manager.generate_day_summaries_range(DAY, 1, self.store, {}, self.app,
                session=self.session if merged else None, force=True)
            self.assertEqual(result["generated"], [DAY])
            self.assertEqual(result["per_date"][0]["message_count"], expected_count)
            self.assert_clean_prompt(["USER_KEPT", "COMPLETE_KEPT"])

    async def test_summary_filters_before_message_cap(self):
        rows = [Message("assistant", "CANCELLED_DROP", TS + i, generation_status="cancelled")
                for i in range(12)]
        rows += [Message("user", "USER_KEPT", TS + 12),
                 Message("assistant", "COMPLETE_KEPT", TS + 13)]
        self.store.save_all(rows)
        self.manager._cfg["day_summary_max_messages"] = 10
        result = await self.manager.generate_day_summary(DAY, self.store, {}, self.app)
        self.assertEqual(result["message_count"], 2)
        self.assertFalse(result["truncated"])
        self.assert_clean_prompt(["USER_KEPT", "COMPLETE_KEPT"])

    async def test_legacy_merger_defaults_without_changing_original_records(self):
        raw = [{"role": "assistant", "content": "legacy", "timestamp": TS}]
        store = SimpleNamespace(get_recent_with_ts=lambda n: raw,
                                get_all=lambda: [SimpleNamespace(**raw[0])])
        session = SimpleNamespace(conversation_store=store)
        for profile in ({}, {"memory_config": {"group_chat_merge_into_history": False}}):
            for rows in (get_merged_recent_messages(session, None, profile, 2),
                         get_merged_messages_for_date(session, None, profile, DAY)):
                self.assertEqual(rows[0]["generation_status"], "completed")
                self.assertIsNone(rows[0]["agent_run_id"])
        self.assertNotIn("generation_status", raw[0])

    async def test_partial_only_skips_model_and_preview_range_counts_match(self):
        self.store.save_all([Message("assistant", "FAILED_DROP", TS, generation_status="failed")])
        self.group.save_all([Message("assistant", "GROUP_FAILED_DROP", TS, generation_status="cancelled")])
        for merged in (False, True):
            options = dict(session=self.session, app=self.app, profile={}) if merged else {}
            preview = self.manager.get_summary_range_preview(self.store, DAY, 1, **options)
            self.assertEqual(preview[0]["message_count"], 0)
            result = await self.manager.generate_day_summaries_range(DAY, 1, self.store, {}, self.app,
                session=self.session if merged else None)
            self.assertEqual(result["no_messages"], [DAY])
        self.assertEqual(await self.manager._extract_facts_llm(self.store.get_recent(), {}, self.app), [])
        self.provider.assert_not_called()
        self.assertEqual(self.calls, [])

    async def test_fact_extraction_keeps_original_dates_with_custom_prompts(self):
        rows = [{"role": "user", "content": "下个礼拜二有面试", "timestamp": TS, "sender": "Human"},
                {"role": "assistant", "content": "一起准备", "timestamp": TS + 86400}]
        self.manager._cfg['extraction_prompt'] = 'Custom rules: {conversation}'
        self.response = '[]'
        await self.manager._extract_facts_llm(rows, {}, self.app)
        prompt = self.calls[-1][0]['content']
        self.assertIn('2026-01-15', prompt)
        self.assertIn('2026-01-16', prompt)
        self.assertIn('下个礼拜二有面试', prompt)
        self.assertIn('YYYY-MM-DD', prompt)

    async def test_chat_background_memory_task_preserves_source_timestamps(self):
        from src.api.chat import _fire_memory_tasks
        self.populate()
        self.manager._cfg['day_summary_enabled'] = False
        expected = {row['content']: row for store in (self.store, self.group)
                    for row in store.get_all_with_id()}
        for merged in (False, True):
            jobs = []
            def capture(coro):
                jobs.append(coro)
            with patch('src.api.chat.asyncio.create_task', side_effect=capture), \
                    patch('src.config.profile_loader.ProfileLoader.load', return_value={
                        'memory_config': {'group_chat_merge_into_history': merged}}), \
                    patch('src.api.memory.get_or_create_manager_for_session', return_value=self.manager), \
                    patch.object(self.manager, 'auto_extract', new=AsyncMock()) as extract:
                _fire_memory_tasks(self.session, self.store, self.app)
                await asyncio.gather(*jobs)
                rows = extract.call_args.args[0]
                self.assertTrue(all('timestamp' in row for row in rows))
                for row in rows:
                    self.assertEqual(row['timestamp'], expected[row['content']]['timestamp'])
                    self.assertEqual(row['generation_status'], expected[row['content']]['generation_status'])

    async def test_unresolved_relative_fact_is_not_saved_or_old_facts_deleted(self):
        previous = self.manager.facts.add('用户有一条应当保持原样的长期事实', source='manual')
        self.response = json.dumps([
            {'content': '用户下个礼拜二需要参加重要面试', 'weight': 1.2},
            {'content': '用户将于2026-01-20参加公司面试', 'weight': 1.2},
        ], ensure_ascii=False)
        await self.manager.auto_extract([{'role': 'user', 'content': '下个礼拜二有面试', 'timestamp': TS}], {}, self.app)
        contents = [f.content for f in self.manager.facts.get_all()]
        self.assertIn(previous.content, contents)
        self.assertIn('用户将于2026-01-20参加公司面试', contents)
        self.assertNotIn('用户下个礼拜二需要参加重要面试', contents)

    async def test_legacy_messages_remain_eligible_and_quiet_summary_still_works(self):
        rows = [{"role": "user", "content": "LEGACY_USER", "timestamp": TS},
                {"role": "assistant", "content": "LEGACY_AI", "timestamp": TS + 1}]
        self.response = "[]"
        await self.manager._extract_facts_llm(rows, {}, self.app)
        self.assert_clean_prompt(["LEGACY_USER", "LEGACY_AI"])
        self.response = "Synthetic summary"
        result = await self.manager.generate_day_summary(DAY, self.store, {}, self.app,
            day_msgs_override=rows, force=True)
        self.assertEqual(result["message_count"], 2)
        self.assert_clean_prompt(["LEGACY_USER", "LEGACY_AI"])
        result = await self.manager.generate_day_summary(DAY, self.store, {}, self.app,
            day_msgs_override=rows, force=True, quiet=True)
        self.assertEqual(result["message_count"], 0)
        self.assertIn("Quiet synthetic day", self.calls[-1][0]["content"])


if __name__ == "__main__":
    unittest.main()
