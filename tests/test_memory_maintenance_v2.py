"""Offline coverage for the daily fact index maintenance entry points."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.core import daily_memory_job as job
from src.memory import memory_changelog, memory_manager
from src.memory.long_term_store import LongTermFact, LongTermStore
from src.memory.vector_store import VectorMemoryStore


PROFILE = "synthetic"
YESTERDAY = "2026-11-04"
NOW = 1000


class MemoryMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())
        self.session = SimpleNamespace(
            profile_id=PROFILE, storage_root=self.root,
            conversation_store=SimpleNamespace(get_all=lambda: []),
        )
        self.app = SimpleNamespace(state=SimpleNamespace(
            session_manager=SimpleNamespace(list_sessions=lambda: [self.session]),
        ))
        self.counts = {"vectors_removed": 3}
        self.manager = SimpleNamespace(
            _cfg={
                "enabled": True,
                "daily_forgetting_enabled": True,
                "daily_reinforcement_enabled": True,
                "daily_consolidation_enabled": True,
                "day_summary_enabled": True,
            },
            maintain_fact_index=AsyncMock(return_value=self.counts),
            preview_fact_index=Mock(return_value={"active": 2, "expired": 1, "vectors_removed": 3}),
            fact_index=object(),
        )
        self.get_manager = self.enterContext(patch.object(job, "_get_manager_for_session", return_value=self.manager))
        self.summary = self.enterContext(patch.object(job, "run_day_summary_for_profile", new_callable=AsyncMock))
        self.portrait = self.enterContext(patch.object(job, "run_portrait_refresh_for_profile", new_callable=AsyncMock))

    async def test_manual_run_calls_only_index_maintenance(self):
        counts = await job.run_forgetting_for_profile(PROFILE, self.app, YESTERDAY, force=True)

        self.assertEqual(counts, self.counts)
        self.manager.maintain_fact_index.assert_awaited_once_with()
        self.summary.assert_not_awaited()
        self.portrait.assert_not_awaited()
        self.assertEqual(list(Path(self.root).iterdir()), [])

    async def test_retired_flags_cannot_enable_maintenance(self):
        self.manager._cfg["daily_forgetting_enabled"] = False

        preview = job.preview_forgetting_for_profile(PROFILE, self.app, YESTERDAY)
        await job.run_forgetting_for_profile(PROFILE, self.app, YESTERDAY)
        await job.run_forgetting_for_profile(PROFILE, self.app, YESTERDAY, force=True)

        self.assertTrue(preview["skip"])
        self.assertEqual(preview["reason_code"], "maintenance_disabled")
        self.manager.preview_fact_index.assert_not_called()
        self.manager.maintain_fact_index.assert_not_awaited()

    async def test_memory_disabled_and_unloaded_profiles_are_skipped(self):
        self.manager._cfg["enabled"] = False

        for profile_id, reason in [(PROFILE, "memory_disabled"), ("missing", "profile_not_loaded")]:
            with self.subTest(profile_id=profile_id):
                preview = job.preview_forgetting_for_profile(profile_id, self.app, YESTERDAY)
                self.assertTrue(preview["skip"])
                self.assertEqual(preview["reason_code"], reason)
                self.assertIsNone(await job.run_forgetting_for_profile(profile_id, self.app, force=True))

        self.manager.maintain_fact_index.assert_not_awaited()

    async def test_preview_maps_manager_counts_without_running_maintenance(self):
        preview = job.preview_forgetting_for_profile(PROFILE, self.app, YESTERDAY)

        self.assertFalse(preview["skip"])
        self.assertEqual(preview["expired_fact_count"], 1)
        self.assertEqual(preview["orphan_cleanup"], {"enabled": True, "would_delete_count": 3})
        for step in ("day_summary", "portrait_refresh", "decay", "reinforcement", "consolidation"):
            self.assertFalse(preview[step]["enabled"], step)
        self.manager.preview_fact_index.assert_called_once_with()
        self.manager.maintain_fact_index.assert_not_awaited()
        self.summary.assert_not_awaited()
        self.portrait.assert_not_awaited()
        self.assertEqual(list(Path(self.root).iterdir()), [])

    async def test_preview_can_count_expiry_without_a_vector_store(self):
        self.manager.fact_index = None
        self.manager.preview_fact_index.return_value = {"active": 0, "expired": 1, "vectors_removed": 0}

        preview = job.preview_forgetting_for_profile(PROFILE, self.app, YESTERDAY)

        self.assertEqual(preview["expired_fact_count"], 1)
        self.assertEqual(preview["orphan_cleanup"], {"enabled": False, "would_delete_count": 0})

    async def test_daily_schedule_keeps_both_model_jobs_and_maintains_quiet_profiles_once(self):
        await job.run_daily_for_profile(PROFILE, YESTERDAY, self.app)
        await job.run_daily_for_profile(PROFILE, YESTERDAY, self.app)

        self.summary.assert_awaited_once_with(PROFILE, YESTERDAY, self.app)
        self.portrait.assert_awaited_once_with(PROFILE, self.app)
        self.manager.maintain_fact_index.assert_awaited_once_with()

    async def test_maintenance_failure_leaves_portrait_independent_and_daily_run_retryable(self):
        self.manager.maintain_fact_index.side_effect = OSError("index unavailable")

        with self.assertRaisesRegex(OSError, "index unavailable"):
            await job.run_daily_for_profile(PROFILE, YESTERDAY, self.app)

        self.portrait.assert_awaited_once_with(PROFILE, self.app)
        self.assertFalse((Path(self.root) / "last_daily_run.json").exists())

    async def test_preview_and_run_agree_using_the_real_manager_and_store(self):
        with patch.object(memory_manager, "log_memory_startup"):
            manager = memory_manager.MemoryManager(PROFILE, self.root, profile_memory_cfg={
                "enabled": True, "daily_forgetting_enabled": True,
            })
        self.get_manager.return_value = manager
        revision, cursors, _ = manager.facts.extraction_snapshot()
        self.assertTrue(manager.facts.apply_extraction(revision, [
            LongTermFact("Active", id="active", kind="stable"),
            LongTermFact("Expired", id="expired", expires_at=NOW),
            LongTermFact("Archived", id="archived", expires_at=NOW - 1, archived=True),
        ], cursors))
        facts_file = Path(self.root) / "long_term_facts.json"
        facts_before = facts_file.read_bytes()

        indexed = {
            "v_active": ("active", "Active"),
            "v_old_revision": ("active", "Previous content"),
            "v_expired": ("expired", "Expired"),
            "v_archived": ("archived", "Archived"),
            "v_missing": ("missing", "Missing"),
            "v_manual": (None, "Manual vector"),
        }

        def get_vectors(*, include):
            return {
                "ids": list(indexed),
                "documents": [document for _, document in indexed.values()],
                "metadatas": [{"fact_id": fid} if fid else {} for fid, _ in indexed.values()],
            }

        def delete_vectors(*, ids):
            for vector_id in ids:
                del indexed[vector_id]

        with patch.object(VectorMemoryStore, "_init"):
            manager.vectors = VectorMemoryStore(self.root, {})
        manager.vectors._available = True
        manager.vectors._collection = SimpleNamespace(get=get_vectors, delete=delete_vectors)

        with patch.object(memory_manager.time, "time", return_value=NOW):
            preview = job.preview_forgetting_for_profile(PROFILE, self.app)
            self.assertEqual(len(indexed), 6)
            result = await job.run_forgetting_for_profile(PROFILE, self.app, force=True)
            after = job.preview_forgetting_for_profile(PROFILE, self.app)

        self.assertEqual(preview["expired_fact_count"], result["expired"])
        self.assertEqual(preview["orphan_cleanup"]["would_delete_count"], result["vectors_removed"])
        self.assertEqual(result, {"active": 1, "expired": 1, "vectors_removed": 4})
        self.assertEqual(after["orphan_cleanup"]["would_delete_count"], 0)
        self.assertEqual(set(indexed), {"v_active", "v_manual"})
        self.assertEqual(facts_file.read_bytes(), facts_before)

    async def test_legacy_changelog_can_still_roll_back(self):
        facts = LongTermStore(self.root)
        source = facts.add("Original fact", weight=0.5)
        summary = facts.add("Previously consolidated summary")
        manager = SimpleNamespace(facts=facts, vectors=None, reembed_fact=AsyncMock(return_value=True))
        memory_changelog.record_run(self.root, {
            "run_id": "legacy",
            "rollback_data": {
                "decay": [{"id": source.id, "old": 1.0, "new": 0.5}],
                "consolidation": {"added": [summary.id], "vectors_removed": [source.id]},
            },
        })
        changelog_path = Path(self.root) / "memory_changelog.json"
        before = changelog_path.read_bytes()
        await job.run_forgetting_for_profile(PROFILE, self.app, force=True)
        self.assertEqual(changelog_path.read_bytes(), before)

        result = await memory_changelog.rollback(self.root, "legacy", manager)

        self.assertEqual(result, {"ok": True, "restored": 3, "skipped": 0})
        self.assertEqual(facts.get_by_id(source.id).weight, 1.0)
        self.assertIsNone(facts.get_by_id(summary.id))
        manager.reembed_fact.assert_awaited_once_with(source.id)
        self.assertTrue(json.loads(changelog_path.read_text(encoding="utf-8"))[0]["rolled_back"])
