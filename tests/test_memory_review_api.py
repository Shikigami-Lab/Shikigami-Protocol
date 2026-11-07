from datetime import datetime
from tempfile import TemporaryDirectory
from time import time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import memory
from src.memory.long_term_store import LongTermFact
from src.memory.memory_manager import MemoryManager
from test_memory_curation_recall import OfflineVectors


class ReviewApiTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = self.enterContext(TemporaryDirectory())
        with patch("src.memory.memory_manager.log_memory_startup"):
            self.manager = MemoryManager("fixture", root)
        self.manager.prune_curation_index = AsyncMock()
        app = FastAPI()
        app.include_router(memory.router)
        self.enterContext(patch.object(memory, "_get_or_create_manager", return_value=self.manager))
        self.client = await self.enterAsyncContext(AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost",
        ))
        now = time()
        self.rows = [
            LongTermFact("Current preference", id="review", source="auto", is_manual=False,
                         review_note="Potentially important"),
            LongTermFact("Latest experience", id="experience", kind="experience", source="auto",
                         is_manual=False, versions=[{"kind": "experience", "content": "Earlier experience"}]),
            LongTermFact("Archived item", id="archive", archived=True, archive_reason="low_value"),
            LongTermFact("Expired plan", id="expired", kind="event", expires_at=now - 3600),
        ]
        revision, cursors, _ = self.manager.facts.extraction_snapshot()
        self.assertTrue(self.manager.facts.apply_extraction(revision, self.rows, cursors))

    async def get_row(self, id):
        response = await self.client.get("/memory/fixture/facts")
        return next(row for row in response.json()["facts"] if row["id"] == id)

    async def test_counts_and_review_keep_then_repeat_is_rejected(self):
        response = await self.client.get("/memory/fixture/status")
        self.assertEqual(response.json()["counts"],
                         {"facts": 1, "experiences": 1, "archived": 1, "expired": 1, "review": 1})
        row = await self.get_row("review")
        body = {"decision": "keep", "expected_revision": row["action_revision"]}
        url = "/memory/fixture/facts/review/review"
        self.assertEqual((await self.client.post(url, json=body)).status_code, 200)
        after = await self.get_row("review")
        self.assertTrue(after["curation_protected"])
        self.assertEqual(after["review_note"], "")
        self.assertEqual((await self.client.post(url, json=body)).status_code, 409)
        self.manager.prune_curation_index.assert_not_called()

    async def test_archive_requires_reason_and_other_profile_cannot_use_token(self):
        row = await self.get_row("review")
        body = {"decision": "archive", "expected_revision": row["action_revision"]}
        url = "/memory/fixture/facts/review/review"
        self.assertEqual((await self.client.post(url, json=body)).status_code, 422)
        body["archive_reason"] = "low_value"
        other = "/memory/other/facts/review/review"
        self.assertEqual((await self.client.post(other, json=body)).status_code, 404)
        self.assertEqual((await self.client.post(url, json=body)).status_code, 200)
        after = await self.get_row("review")
        self.assertTrue(after["archived"])
        self.assertEqual(after["archive_reason"], "low_value")
        self.manager.prune_curation_index.assert_awaited_once()

    async def test_identical_records_in_different_profiles_have_distinct_action_tokens(self):
        root = self.enterContext(TemporaryDirectory())
        with patch("src.memory.memory_manager.log_memory_startup"):
            other = MemoryManager("other", root)
        revision, cursors, _ = other.facts.extraction_snapshot()
        self.assertTrue(other.facts.apply_extraction(revision, self.rows, cursors))
        original = await self.get_row("review")
        with patch.object(memory, "_get_or_create_manager", return_value=other):
            response = await self.client.get("/memory/other/facts")
            other_row = next(row for row in response.json()["facts"] if row["id"] == "review")
            self.assertNotEqual(other_row["action_revision"], original["action_revision"])
            refused = await self.client.post("/memory/other/facts/review/review", json={
                "decision": "keep", "expected_revision": original["action_revision"],
            })
            self.assertEqual(refused.status_code, 409)
        self.assertTrue(other.facts.get_by_id("review").review_note)

    async def test_read_only_page_can_be_promoted_and_applied_without_a_second_model_call(self):
        revision, cursors, facts = self.manager.facts.extraction_snapshot()
        facts.append(LongTermFact("Waved once.", id="transient", source="auto", is_manual=False))
        self.assertTrue(self.manager.facts.apply_extraction(revision, facts, cursors))
        original = await self.get_row("transient")
        async def suggest(rows, _preset):
            return [
                {"fact_id": fact.id, "action": "archive", "archive_reason": "ephemeral",
                 "future_use": "", "evidence_quote": fact.content, "reason": "Only a momentary action"}
                for fact in rows
            ]

        base = "/memory/fixture/curation"
        with patch.object(memory, "request_fixed_review", new=AsyncMock(side_effect=suggest)) as model, \
             patch.object(memory, "curation_preset", return_value={"model": "fixture"}):
            page = await self.client.post(base + "/bulk-preview", json={})
            self.assertEqual(page.status_code, 200, page.text)
            model.assert_not_awaited()
            page = await self.client.post(base + "/bulk-preview", json={"fact_id": "transient"})
            self.assertEqual(page.status_code, 200, page.text)
            token = page.json()["page_token"]
            self.assertEqual(await self.get_row("transient"), original)
            promoted = await self.client.post(base + "/bulk-preview/promote",
                                              json={"page_token": token, "fact_id": "transient"})
            self.assertEqual(promoted.status_code, 200, promoted.text)
            self.assertEqual(await self.get_row("transient"), original)
            applied = await self.client.post(base + "/apply", json={
                "preview_id": promoted.json()["preview_id"], "fact_ids": ["transient"],
            })
            self.assertEqual(applied.status_code, 200, applied.text)
            model.assert_awaited_once()
        self.assertTrue((await self.get_row("transient"))["archived"])

    async def test_fact_and_archive_views_partition_current_records(self):
        facts = (await self.client.get("/memory/fixture/facts?view=facts")).json()["facts"]
        archive = (await self.client.get("/memory/fixture/facts?view=archive")).json()["facts"]
        self.assertEqual({row["id"] for row in facts}, {"review", "experience"})
        self.assertEqual({row["id"] for row in archive}, {"archive", "expired"})
        self.assertTrue(all(row["recallable"] for row in facts))
        self.assertTrue(all(not row["recallable"] for row in archive))

    async def test_profile_managers_share_the_same_vector_mutation_lock(self):
        with patch("src.memory.memory_manager.log_memory_startup"):
            second = MemoryManager("fixture", self.manager._storage_root)
        self.assertIs(self.manager._fact_index_lock, second._fact_index_lock)

    async def test_excerpt_apply_leaves_old_fact_when_staging_fails(self):
        revision, cursors, facts = self.manager.facts.extraction_snapshot()
        original = "用户想用显卡运行本地模型。随后聊了几句闲话。"
        facts.append(LongTermFact(original, id="trim", source="auto", is_manual=False))
        self.assertTrue(self.manager.facts.apply_extraction(revision, facts, cursors))
        excerpt = "用户想用显卡运行本地模型。"
        model = AsyncMock(return_value=[{
            "fact_id": "trim", "action": "update", "reason": "保留硬件用途",
            "future_use": "询问本地模型硬件", "evidence_quote": excerpt, "keep_text": excerpt,
        }])
        base = "/memory/fixture/curation"
        with patch.object(memory, "request_fixed_review", model), \
             patch.object(memory, "curation_preset", return_value={"model": "fixture"}):
            page = await self.client.post(base + "/bulk-preview", json={"fact_id": "trim"})
            preview = await self.client.post(base + "/bulk-preview/promote", json={
                "page_token": page.json()["page_token"], "fact_id": "trim",
            })
        self.manager.vectors = SimpleNamespace(is_available=lambda: True, stage_fact=lambda *_: None)
        applied = await self.client.post(base + "/apply", json={
            "preview_id": preview.json()["preview_id"], "fact_ids": ["trim"],
        })
        self.assertEqual(applied.status_code, 409, applied.text)
        current = self.manager.facts.get_by_id("trim")
        self.assertEqual(current.content, original)
        self.assertEqual(current.versions, [])

    async def test_restore_old_version_uses_index_and_preserves_displaced_current(self):
        self.manager.vectors = SimpleNamespace(is_available=lambda: True)
        self.manager.reembed_fact = AsyncMock(return_value=True)
        row = await self.get_row("experience")
        url = "/memory/fixture/facts/experience/versions/restore"
        body = {"expected_revision": row["action_revision"], "version_index": 0}
        response = await self.client.post(url, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        after = await self.get_row("experience")
        self.assertEqual(after["content"], "Earlier experience")
        self.assertEqual(after["versions"][-1]["content"], "Latest experience")
        self.manager.reembed_fact.assert_awaited_once_with("experience")
        self.assertEqual((await self.client.post(url, json=body)).status_code, 409)
        stale_edit = await self.client.put("/memory/fixture/facts/experience", json={
            "content": "A stale draft", "expected_revision": row["action_revision"],
        })
        self.assertEqual(stale_edit.status_code, 409)
        self.assertEqual((await self.get_row("experience"))["content"], "Earlier experience")

    async def test_diary_links_follow_current_evidence_after_restore(self):
        first = {"source": "single", "id": 1, "timestamp": datetime(2026, 1, 15, 12).timestamp()}
        second = {"source": "single", "id": 2, "timestamp": datetime(2026, 1, 16, 12).timestamp()}
        revision, cursors, facts = self.manager.facts.extraction_snapshot()
        episode = next(fact for fact in facts if fact.id == "experience")
        episode.evidence = [second]
        episode.versions[0]["evidence"] = [first]
        self.assertTrue(self.manager.facts.apply_extraction(revision, facts, cursors))
        self.manager.day_store.add_summary("2026-01-15", "Earlier note")
        self.manager.day_store.add_summary("2026-01-16", "Later note", experience_ids=["experience"])

        async def links():
            response = await self.client.get("/memory/fixture/summaries")
            return {row["date"]: row["experience_ids"] for row in response.json()["summaries"]}

        self.assertEqual(await links(), {"2026-01-15": [], "2026-01-16": ["experience"]})
        current = await self.get_row("experience")
        response = await self.client.post("/memory/fixture/facts/experience/versions/restore", json={
            "expected_revision": current["action_revision"], "version_index": 0,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await links(), {"2026-01-15": ["experience"], "2026-01-16": []})

    async def test_fact_crud_updates_the_index_through_the_normal_api(self):
        manager = self.manager
        manager.vectors = OfflineVectors()
        base = "/memory/fixture"
        created = await self.client.post(f"{base}/facts", json={"content": "Tea without sugar"})
        self.assertEqual(created.status_code, 201)
        fact_id = created.json()["id"]
        vectors = (await self.client.get(f"{base}/vectors")).json()["items"]
        self.assertEqual([(row["metadata"]["fact_id"], row["document"]) for row in vectors],
                         [(fact_id, "Tea without sugar")])
        self.assertEqual((await self.client.put(f"{base}/facts/{fact_id}",
                                               json={"content": "Tea with sugar"})).status_code, 200)
        [updated] = manager.vectors.rows
        self.assertEqual(updated["id"], vectors[0]["id"])
        self.assertEqual(updated["document"], "Tea with sugar")
        self.assertEqual((await self.client.delete(f"{base}/facts/{fact_id}")).status_code, 200)
        self.assertEqual(manager.vectors.rows, [])
        self.assertIsNone(manager.facts.get_by_id(fact_id))
