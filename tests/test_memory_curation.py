import asyncio
import multiprocessing
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

from src.memory.curation import MemoryCuration, CurationConflict
from src.memory.long_term_store import LongTermFact, LongTermStore
from src.memory.memory_manager import MemoryManager


def fact(id, content, **kwargs):
    return LongTermFact(id=id, content=content, source="auto", is_manual=False, **kwargs)


def action(id, op, **kwargs):
    return {"fact_id": id, "action": op, "reason": "test evidence", **kwargs}


def _competing_writer(root, entered, release, results, first):
    store = LongTermStore(root)
    revision, cursors, facts, state = store.curation_snapshot()
    state["writer"] = first
    if first:
        write = store._write_current_file
        def hold(*args):
            entered.set()
            if not release.wait(8):
                raise RuntimeError("Timed out waiting for concurrent writer")
            return write(*args)
        store._write_current_file = hold
    else:
        entered.set()
    results.put(store.apply_curation(revision, facts, cursors, state))


class CurationProcessTests(TestCase):
    def test_competing_processes_cannot_overwrite_a_reservation(self):
        root = self.enterContext(TemporaryDirectory())
        context = multiprocessing.get_context("spawn")
        first_entered, second_entered, release = context.Event(), context.Event(), context.Event()
        results = context.Queue()
        first = context.Process(target=_competing_writer, args=(root, first_entered, release, results, True))
        second = context.Process(target=_competing_writer, args=(root, second_entered, release, results, False))
        first.start()
        try:
            self.assertTrue(first_entered.wait(8))
            second.start()
            try:
                self.assertTrue(second_entered.wait(8))
                release.set()
                self.assertEqual(sorted([results.get(timeout=8), results.get(timeout=8)]), [False, True])
            finally:
                second.join(timeout=8)
                if second.is_alive():
                    second.terminate()
                    second.join()
        finally:
            release.set()
            first.join(timeout=8)
            if first.is_alive():
                first.terminate()
                first.join()
        self.assertEqual((first.exitcode, second.exitcode), (0, 0))
        self.assertIs(LongTermStore(root).curation_snapshot()[-1]["writer"], True)


class CurationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())
        self.store = LongTermStore(self.root)
        revision, cursors, _ = self.store.extraction_snapshot()
        self.assertTrue(self.store.apply_extraction(revision, [
            fact("a", "Likes coffee", category="preference"),
            fact("b", "Enjoys coffee", category="preference"),
            fact("c", "Waved once"),
            fact("d", "Said good night once", pinned=True),
            fact("e", "Will wave", kind="event", event_date="2026-11-06"),
            fact("f", "Waved", kind="event", event_date="2026-11-06"),
        ], cursors))
        self.service = MemoryCuration("test", self.store)

    async def preview(self, *actions):
        async def reply(rows):
            proposed = {item["fact_id"]: item for item in actions}
            return [proposed.get(row.id, action(row.id, "keep")) for row in rows]
        return await self.service.preview(reply)

    async def test_preview_apply_replay_and_restore(self):
        preview = await self.preview(
            action("b", "merge", target_id="a"),
            action("c", "archive", archive_reason="low_value"),
        )
        self.assertFalse(self.store.get_by_id("c").archived)
        self.assertEqual(preview["profile_id"], "test")
        receipt = self.service.apply(preview["preview_id"])
        self.assertEqual((receipt["merged"], receipt["archived"]), (1, 1))
        reopened = LongTermStore(self.root)
        merged = reopened.get_by_id("b")
        self.assertEqual((merged.merged_into, merged.content), ("a", "Enjoys coffee"))
        self.assertEqual(reopened.get_by_id("c").archive_reason, "low_value")
        self.assertEqual(MemoryCuration("test", reopened).apply(preview["preview_id"]), receipt)
        self.service.restore("b", merged.archive_revision)
        self.assertFalse(reopened.get_by_id("b").archived)
        with self.assertRaises(CurationConflict):
            self.service.restore("b", merged.archive_revision)
        next_preview = await self.preview(action("b", "merge", target_id="a"))
        self.assertNotIn("b", {item["fact_id"] for item in next_preview["items"]})

    async def test_snapshot_conflict_does_not_partially_apply(self):
        preview = await self.preview(action("b", "merge", target_id="a"),
                                     action("c", "archive", archive_reason="low_value"))
        other = LongTermStore(self.root)
        other.update("a", content="Dislikes coffee")
        with self.assertRaises(CurationConflict):
            self.service.apply(preview["preview_id"])
        self.assertFalse(other.get_by_id("c").archived)

    async def test_event_without_shared_evidence_goes_to_review(self):
        preview = await self.preview(action("e", "merge", target_id="f"))
        event = next(item for item in preview["items"] if item["fact_id"] == "e")
        self.assertEqual(event["action"], "review")
        self.service.apply(preview["preview_id"])
        self.assertTrue(self.store.get_by_id("e").review_note)
        self.assertFalse(self.store.get_by_id("e").archived)
        self.assertFalse(self.store.get_by_id("d").archived)

    async def test_partial_or_foreign_reply_is_rejected_without_changes(self):
        with self.assertRaises(ValueError):
            await self.service.preview(AsyncMock(return_value=[action("alien", "archive")]))
        self.assertEqual(len(self.store.get_active()), 6)

    async def test_daily_budget_survives_reload_and_only_applies_safe_changes(self):
        async def decide(rows):
            choices = {
                "b": action("b", "merge", target_id="a"),
                "c": action("c", "archive", archive_reason="low_value"),
            }
            return [choices.get(row.id, action(row.id, "keep")) for row in rows]
        model = AsyncMock(side_effect=decide)
        await self.service.run_daily("2026-11-07", model)
        reopened = MemoryCuration("test", LongTermStore(self.root))
        await reopened.run_daily("2026-11-07", model)
        await reopened.run_daily("2026-11-08", model)
        model.assert_awaited_once()
        self.assertEqual(reopened.status()["daily"]["status"], "completed")
        self.assertTrue(reopened.store.get_by_id("c").archived)
        self.assertFalse(reopened.store.get_by_id("b").archived)
        self.assertTrue(reopened.store.get_by_id("b").review_note)

    async def test_failure_consumes_daily_attempt_across_two_managers(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def fail(rows):
            entered.set()
            await release.wait()
            raise ValueError("model failure")
        model = AsyncMock(side_effect=fail)
        task = asyncio.create_task(self.service.run_daily("2026-11-07", model))
        await entered.wait()
        another = MemoryCuration("test", LongTermStore(self.root))
        await another.run_daily("2026-11-07", model)
        release.set()
        await task
        model.assert_awaited_once()
        self.assertEqual(another.status()["daily"]["status"], "failed")

    async def test_automatic_disable_while_awaiting_model_blocks_apply(self):
        enabled = True
        async def decide(rows):
            nonlocal enabled
            enabled = False
            return [action(row.id, "archive", archive_reason="low_value") for row in rows]
        await self.service.run_daily("2026-11-07", decide, enabled=lambda: enabled)
        self.assertFalse(self.store.get_by_id("c").archived)
        self.assertEqual(self.service.status()["daily"]["status"], "cancelled")

    async def test_manual_apply_respects_selection(self):
        preview = await self.preview(action("b", "merge", target_id="a"),
                                     action("c", "archive", archive_reason="low_value"))
        self.service.apply(preview["preview_id"], ["c"])
        self.assertTrue(self.store.get_by_id("c").archived)
        self.assertFalse(self.store.get_by_id("b").archived)
        with self.assertRaises(CurationConflict):
            self.service.apply(preview["preview_id"], ["b"])
        self.assertIn("b", {item["fact_id"] for item in (await self.preview())["items"]})

    async def test_index_cleanup_can_resume_after_failed_delete(self):
        preview = await self.preview(action("c", "archive", archive_reason="low_value"))
        self.service.apply(preview["preview_id"])
        manager = MemoryManager("test", self.root, profile_memory_cfg={"vector_enabled": False})
        manager.vectors = Mock()
        manager.vectors.is_available.return_value = True
        manager.vectors.get_all.return_value = [{"id": "vc", "metadata": {"fact_id": "c"}, "document": "Waved once"}]
        manager.vectors.delete.side_effect = [False, True]
        await manager.prune_curation_index()
        self.assertTrue(manager.facts.curation_status()["index_pending"])
        await manager.prune_curation_index()
        self.assertFalse(manager.facts.curation_status()["index_pending"])

    async def test_daily_can_apply_a_proven_exact_duplicate(self):
        revision, cursors, facts = self.store.extraction_snapshot()
        facts.append(fact("copy", "Likes coffee.", category="preference"))
        self.assertTrue(self.store.apply_extraction(revision, facts, cursors))

        async def decide(rows):
            return [action(row.id, "merge", target_id="a") if row.id == "copy"
                    else action(row.id, "keep") for row in rows]

        await self.service.run_daily("2026-11-07", decide)
        copy = self.store.get_by_id("copy")
        self.assertTrue(copy.archived)
        self.assertEqual(copy.merged_into, "a")
        self.assertTrue(copy.merged_source_version and copy.merged_target_version)
        self.assertEqual(self.service.status()["latest"]["counts"]["merged"], 1)

    async def test_untyped_legacy_relationship_and_future_plan_cannot_be_auto_archived(self):
        revision, cursors, facts = self.store.extraction_snapshot()
        facts.append(fact("legacy", "User emergency contact is Alex", category="relationship"))
        self.assertTrue(self.store.apply_extraction(revision, facts, cursors))
        async def decide(rows):
            return [action(row.id, "archive", archive_reason="cold_memory") for row in rows]
        await self.service.run_daily("2026-11-07", decide)
        for id in ("legacy", "e"):
            self.assertFalse(self.store.get_by_id(id).archived)
            self.assertTrue(self.store.get_by_id(id).review_note)

    async def test_preview_concurrency_is_rejected(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def reply(rows):
            entered.set()
            await release.wait()
            return [action(row.id, "keep") for row in rows]
        in_flight = asyncio.create_task(self.service.preview(reply))
        await entered.wait()
        with self.assertRaises(CurationConflict):
            await self.service.preview(reply)
        release.set()
        await in_flight
