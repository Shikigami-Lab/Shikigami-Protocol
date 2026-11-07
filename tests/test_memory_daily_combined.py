"""Offline acceptance of the scheduled one-request memory transaction."""
import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from src.core import daily_memory_job as job
from src.llm.openai_compat import OpenAICompatProvider
from src.memory.conversation_store import ConversationStore
from src.memory.extraction_batches import message_hash
from src.memory.long_term_store import LongTermFact, LongTermStore
from src.memory.memory_manager import MemoryManager


DAY = "2026-11-07"
PROFILE = "fixture"


class DailyCombinedMemoryTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())
        self.enterContext(patch("src.memory.memory_manager.log_memory_startup"))
        self.manager = MemoryManager(PROFILE, self.root, profile_memory_cfg={
            "enabled": True, "daily_curation_enabled": True, "extraction_llm_preset": "memory",
        })
        self.store = ConversationStore(self.root + "/chat", max_turns=0)
        self.addCleanup(self.store.close)
        self.session = SimpleNamespace(profile_id=PROFILE, storage_root=self.root, conversation_store=self.store)
        self.preset = {"model": "fixture-memory"}
        self.app = SimpleNamespace(state=SimpleNamespace(
            config=SimpleNamespace(llm_presets={"memory": self.preset}),
            session_manager=SimpleNamespace(list_sessions=lambda: [self.session]),
        ))
        self.enterContext(patch.object(job, "_get_manager_for_session", return_value=self.manager))
        self.enterContext(patch("src.config.profile_loader.ProfileLoader.load", return_value={}))
        self.provider = object.__new__(OpenAICompatProvider)
        self.provider.client = Mock()
        self.provider.client.close = AsyncMock()
        self.provider.client.with_options.return_value = self.provider.client
        self.sdk_options = self.provider.client.with_options
        self.get_provider = self.enterContext(patch("src.llm.registry.get_provider", return_value=self.provider))
        self.calls = []

    def seed(self, *, text="I drink unsweetened tea.", registered=True, old=True, offset=0):
        self.user_id = self.store.append("user", text)
        self.assistant_id = self.store.append("assistant", "Thanks for telling me.")
        revision, _, _, _ = self.manager.facts.extraction_progress_snapshot()
        facts = [LongTermFact("Waved once", id="old", source="auto", is_manual=False)] if old else []
        cursors = {"single": 0} if registered else {}
        offsets = {"single": {"id": self.user_id, "offset": offset,
                              "content_hash": message_hash(text)}} if offset else {}
        self.assertTrue(self.manager.facts.apply_extraction_progress(revision, facts, cursors, offsets))
        return cursors, offsets

    def seed_assistant_prefix(self, size):
        earlier_user = self.store.append("user", "Hello.")
        text = "x" * size
        assistant = self.store.append("assistant", text)
        self.seed()
        revision, _, facts, _ = self.manager.facts.extraction_progress_snapshot()
        self.assertTrue(self.manager.facts.apply_extraction_progress(
            revision, facts, {"single": earlier_user}, {"single": {
                "id": assistant, "offset": 32_000, "content_hash": message_hash(text),
            }},
        ))
        return earlier_user, assistant, message_hash(text)

    def reply(self, response):
        async def stream(messages):
            self.calls.append(messages)
            value = response(messages) if callable(response) else response
            if isinstance(value, str):
                yield value
            else:
                yield json.dumps(value)
        self.provider.stream_chat = stream

    def archive(self):
        return {"fact_id": "old", "action": "archive", "archive_reason": "low_value",
                "reason": "A momentary action with no future relevance."}

    def add(self):
        return {"op": "add", "kind": "stable", "category": "preference",
                "content": "They prefer unsweetened tea.", "evidence": [f"single:{self.user_id}"]}

    async def run_job(self, day=DAY):
        await job._run_daily_curation(self.session, day, self.app)

    async def test_one_scheduled_request_commits_both_and_leaves_diary_separate(self):
        self.seed()
        self.reply({"operations": [self.add()], "items": [self.archive()]})
        with patch.object(job, "date") as clock, \
             patch.object(job, "run_day_summary_for_profile", new_callable=AsyncMock) as diary, \
             patch.object(job, "run_portrait_refresh_for_profile", new_callable=AsyncMock):
            clock.today.return_value.isoformat.return_value = DAY
            clock.today.return_value.strftime.return_value = DAY
            await job.run_daily_for_profile(PROFILE, "2026-11-06", self.app)
            diary.assert_awaited_once_with(PROFILE, "2026-11-06", self.app)
        await self.run_job()
        self.get_provider.assert_called_once_with(self.preset)
        self.sdk_options.assert_called_once_with(max_retries=0, timeout=60.0)
        self.assertEqual(len(self.calls), 1)
        self.assertIn(f"[single:{self.user_id}]", self.calls[0][-1]["content"])
        self.assertIn('"fact_id": "old"', self.calls[0][-1]["content"])
        store = LongTermStore(self.root)
        self.assertTrue(store.get_by_id("old").archived)
        self.assertEqual([fact.content for fact in store.get_active()], ["They prefer unsweetened tea."])
        self.assertEqual(store.source_cursors(), {"single": self.assistant_id})
        self.assertEqual(store.curation_status()["daily"]["status"], "completed")

    async def test_empty_dialogue_operations_commit_cursor_and_clear_completed_offset(self):
        self.seed(offset=3)
        self.reply({"operations": [], "items": [self.archive()]})
        await self.run_job()
        self.assertEqual(len(self.calls), 1)
        _, cursors, _, offsets = LongTermStore(self.root).extraction_progress_snapshot()
        self.assertEqual((cursors, offsets), ({"single": self.assistant_id}, {}))
        self.assertTrue(self.manager.facts.get_by_id("old").archived)

    async def test_empty_dialogue_operations_commit_partial_offset(self):
        text = "Hello. " * 5000
        self.seed(text=text, offset=3)
        self.reply({"operations": [], "items": [self.archive()]})
        await self.run_job()
        _, cursors, _, offsets = LongTermStore(self.root).extraction_progress_snapshot()
        self.assertEqual(cursors, {"single": 0})
        self.assertEqual(offsets, {"single": {
            "id": self.user_id, "offset": 32_003, "content_hash": message_hash(text),
        }})
        self.assertTrue(self.manager.facts.get_by_id("old").archived)

    async def test_missing_or_invalid_operations_preserve_progress_and_curation(self):
        baseline = self.seed(offset=3)
        responses = (
            {"items": [self.archive()]},
            {"operations": {}, "items": [self.archive()]},
        )
        for day, response in enumerate(responses, start=7):
            with self.subTest(response=response):
                self.reply(response)
                await self.run_job(f"2026-11-{day:02}")
                _, cursors, _, offsets = LongTermStore(self.root).extraction_progress_snapshot()
                self.assertEqual((cursors, offsets), baseline)
                self.assertFalse(self.manager.facts.get_by_id("old").archived)
                self.assertEqual(self.manager.facts.curation_status()["daily"]["status"], "failed")

    async def test_model_failure_consumes_budget_without_advancing_or_curating(self):
        self.seed()
        self.reply("[LLM Error: synthetic]")
        await self.run_job()
        self.manager.facts = LongTermStore(self.root)
        self.manager.curation = None
        await self.run_job()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.manager.facts.curation_status()["daily"]["status"], "failed")
        self.assertFalse(self.manager.facts.get_by_id("old").archived)
        self.assertEqual(self.manager.facts.source_cursors(), {"single": 0})

    async def test_invalid_dialogue_rejects_otherwise_valid_curation(self):
        self.seed()
        self.reply({"operations": [{**self.add(), "evidence": ["single:999"]}],
                    "items": [self.archive()]})
        await self.run_job()
        self.assertEqual(self.manager.facts.curation_status()["daily"]["status"], "failed")
        self.assertFalse(self.manager.facts.get_by_id("old").archived)
        self.assertEqual(self.manager.facts.source_cursors(), {"single": 0})

    async def test_overlapping_changes_reject_both_sides(self):
        self.seed(text="I remember that you waved once, which helped me feel welcome.")
        self.reply({"operations": [{**self.add(), "op": "update", "fact_id": "old",
                                    "category": "other", "content": "I waved and they felt welcome."}],
                    "items": [self.archive()]})
        await self.run_job()
        old = self.manager.facts.get_by_id("old")
        self.assertEqual((old.content, old.archived), ("Waved once", False))
        self.assertEqual(self.manager.facts.source_cursors(), {"single": 0})
        self.assertIn(self.manager.facts.curation_status()["daily"]["status"], ("stale", "failed"))

    async def test_competing_progress_rejects_combined_response(self):
        self.seed()

        def reply(_):
            other = LongTermStore(self.root)
            revision, _, facts = other.extraction_snapshot()
            self.assertTrue(other.apply_extraction(revision, facts, {"single": self.user_id}))
            return {"operations": [self.add()], "items": [self.archive()]}

        self.reply(reply)
        await self.run_job()
        self.assertEqual(self.manager.facts.source_cursors(), {"single": self.user_id})
        self.assertFalse(self.manager.facts.get_by_id("old").archived)
        self.assertEqual(self.manager.facts.count(), 1)
        self.assertIn(self.manager.facts.curation_status()["daily"]["status"], ("stale", "failed"))

    async def test_unregistered_dialogue_is_not_sent_with_curation_only(self):
        self.seed(registered=False)
        self.reply({"items": [self.archive()]})
        await self.run_job()
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("unsweetened tea", self.calls[0][-1]["content"])
        self.assertTrue(self.manager.facts.get_by_id("old").archived)
        self.assertEqual(self.manager.facts.source_cursors(), {})

    async def test_registered_dialogue_uses_budget_even_with_no_curation_candidates(self):
        self.seed(old=False)
        self.reply({"operations": [self.add()], "items": []})
        await self.run_job()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([fact.content for fact in self.manager.facts.get_all()], ["They prefer unsweetened tea."])
        self.assertEqual(self.manager.facts.source_cursors(), {"single": self.assistant_id})

    async def test_manual_preview_flags_the_synthetic_correction_wording(self):
        self.seed()
        revision, cursors, facts = self.manager.facts.extraction_snapshot()
        facts[0].content = "\u5bf9\u65b9\u6307\u6b63\u8fc7\u90a3\u6bb5\u63cf\u8ff0"
        self.assertTrue(self.manager.facts.apply_extraction(revision, facts, cursors))
        preview = await job.curation_for(self.manager).preview(AsyncMock(return_value=[self.archive()]))
        self.assertEqual(preview["items"][0]["action"], "review")

    async def test_partial_dialogue_commits_new_offset_with_curation(self):
        text = "I drink unsweetened tea. " + "Background detail. " * 2000
        self.seed(text=text)
        proposal = {**self.add(), "evidence": [f"single:{self.user_id}#0-6000"]}
        self.reply({"operations": [proposal], "items": [self.archive()]})
        await self.run_job()
        store = LongTermStore(self.root)
        _, cursors, _, offsets = store.extraction_progress_snapshot()
        self.assertEqual(cursors, {"single": 0})
        self.assertEqual(offsets, {"single": {
            "id": self.user_id, "offset": 32_000, "content_hash": message_hash(text),
        }})
        self.assertTrue(store.get_by_id("old").archived)
        evidence = store.get_active()[0].evidence[0]
        self.assertEqual((evidence["start_char"], evidence["end_char"]), (0, 6000))

    async def test_reply_that_stops_being_completed_invalidates_both_outputs(self):
        self.seed()

        def reply(_):
            messages = self.store.get_all()
            messages[-1].generation_status = "cancelled"
            self.store.save_all(messages)
            return {"operations": [self.add()], "items": [self.archive()]}

        self.reply(reply)
        await self.run_job()
        self.assertEqual(self.manager.facts.curation_status()["daily"]["status"], "stale")
        self.assertEqual(self.manager.facts.source_cursors(), {"single": 0})
        self.assertFalse(self.manager.facts.get_by_id("old").archived)

    async def test_group_revocation_invalidates_both_outputs(self):
        self.seed()
        group = ConversationStore(self.root + "/group", max_turns=0)
        self.addCleanup(group.close)
        group_user = group.append("user", "I drink unsweetened tea.")
        revision, _, facts, _ = self.manager.facts.extraction_progress_snapshot()
        self.assertTrue(self.manager.facts.apply_extraction_progress(revision, facts, {"group:g": 0}, {}))
        members = ["g"]
        self.app.state.group_manager = SimpleNamespace(
            get_group_ids_for_profile=lambda _: list(members),
            get_conversation_store=lambda _: group,
        )

        def reply(messages):
            self.assertIn(f"[group:g:{group_user}]", messages[-1]["content"])
            members.clear()
            return {"operations": [{**self.add(), "evidence": [f"group:g:{group_user}"]}],
                    "items": [self.archive()]}

        self.reply(reply)
        await self.run_job()
        await self.run_job()
        store = LongTermStore(self.root)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(store.curation_status()["daily"]["status"], "stale")
        self.assertEqual(store.source_cursors(), {"group:g": 0})
        self.assertEqual([fact.id for fact in store.get_active()], ["old"])

    async def test_assistant_only_prefix_advances_locally_before_the_single_combined_call(self):
        earlier_user, assistant, digest = self.seed_assistant_prefix(70_000)

        def reply(messages):
            _, cursors, _, offsets = LongTermStore(self.root).extraction_progress_snapshot()
            self.assertEqual(cursors, {"single": earlier_user})
            self.assertEqual(offsets, {"single": {"id": assistant, "offset": 64_000, "content_hash": digest}})
            self.assertIn(f"[single:{self.user_id}]", messages[-1]["content"])
            return {"operations": [self.add()], "items": [self.archive()]}

        self.reply(reply)
        await self.run_job()
        await self.run_job()
        store = LongTermStore(self.root)
        self.assertEqual(len(self.calls), 1)
        _, cursors, facts, offsets = store.extraction_progress_snapshot()
        self.assertEqual((cursors, offsets), ({"single": self.assistant_id}, {}))
        self.assertEqual([fact.content for fact in facts if fact.is_active()], ["They prefer unsweetened tea."])

    async def test_old_record_review_can_continue_after_bounded_assistant_only_progress(self):
        earlier_user, assistant, digest = self.seed_assistant_prefix(110_000)

        def reply(messages):
            self.assertNotIn(f"[single:{self.user_id}]", messages[-1]["content"])
            return {"items": [self.archive()]}

        self.reply(reply)
        await self.run_job()
        store = LongTermStore(self.root)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(store.get_by_id("old").archived)
        _, cursors, _, offsets = store.extraction_progress_snapshot()
        self.assertEqual(cursors, {"single": earlier_user})
        self.assertEqual(offsets, {"single": {"id": assistant, "offset": 96_000, "content_hash": digest}})

        self.reply({"operations": [self.add()], "items": []})
        await self.run_job("2026-11-08")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(store.source_cursors(), {"single": self.assistant_id})
        self.assertEqual([fact.content for fact in store.get_active()], ["They prefer unsweetened tea."])

    async def test_invalid_curation_rejects_otherwise_valid_dialogue(self):
        self.seed()
        self.reply({"operations": [self.add()], "items": []})
        await self.run_job()
        self.assertEqual(self.manager.facts.curation_status()["daily"]["status"], "failed")
        self.assertEqual(self.manager.facts.source_cursors(), {"single": 0})
        self.assertEqual(self.manager.facts.count(), 1)
