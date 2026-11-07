from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
import time

from src.memory.curation import CurationConflict, MemoryCuration
from src.memory.fact_operations import apply_operations
from src.memory.long_term_store import LongTermFact, LongTermStore
from src.memory.recall import RecallCandidates


def note(fact_id, content, **fields):
    return LongTermFact(id=fact_id, content=content, source="auto", is_manual=False, **fields)


def propose_archive(reason):
    async def decide(rows):
        return [
            {"fact_id": row.id, "action": "archive", "archive_reason": reason,
             "reason": f"Proposed cleanup for {row.id}"}
            for row in rows
        ]
    return AsyncMock(side_effect=decide)


class ArchiveProtectionTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())
        self.store = LongTermStore(self.root)
        self.service = MemoryCuration("fixture", self.store)

    def seed(self, *notes):
        revision, cursors, _ = self.store.extraction_snapshot()
        self.assertTrue(self.store.apply_extraction(revision, list(notes), cursors))

    async def test_daily_requires_affirmative_whole_note_proof_for_legacy_cleanup(self):
        self.seed(
            note("mixed", "我挥了挥手，用户明确纠正：我平时也戴眼镜。"),
            note("joined", "Waved once and clarified the shape of my ears."),
            note("unknown", "The regular pair is round, as we established."),
            note("history", "Waved once", versions=[{"content": "User wears glasses every day"}]),
            note("wave", "Waved once."),
            note("roleplay", "我坐下揉尾巴。"),
        )
        model = propose_archive("low_value")
        await self.service.run_daily("2026-11-07", model)

        reopened = MemoryCuration("fixture", LongTermStore(self.root))
        for fact_id in ("mixed", "joined", "unknown", "history"):
            with self.subTest(fact_id=fact_id):
                fact = reopened.store.get_by_id(fact_id)
                self.assertTrue(fact.is_active())
                self.assertIn(f"Proposed cleanup for {fact_id}", fact.review_note)
        for fact_id in ("wave", "roleplay"):
            with self.subTest(fact_id=fact_id):
                fact = reopened.store.get_by_id(fact_id)
                self.assertTrue(fact.archived)
                self.assertEqual(fact.archive_reason, "low_value")
                self.assertEqual(fact.archive_note, f"Proposed cleanup for {fact_id}")
                self.assertTrue(fact.archive_revision)

        await reopened.run_daily("2026-11-07", model)
        await reopened.run_daily("2026-11-08", model)
        model.assert_awaited_once()

    async def test_preview_flags_mixed_correction_and_applies_only_reversible_outcomes(self):
        self.seed(
            note("mixed", "I waved once. The user corrected me: I wear glasses in real life."),
            note("appearance", "I nodded once. The user actually has green eyes."),
            note("wave", "Waved once."),
        )
        path = Path(self.root, "long_term_facts.json")
        original = path.read_bytes()
        preview = await self.service.preview(propose_archive("low_value"))
        self.assertEqual(path.read_bytes(), original)
        items = {item["fact_id"]: item for item in preview["items"]}
        self.assertEqual(items["mixed"]["action"], "review")
        self.assertIn("correction", items["mixed"]["reason"].lower())
        self.assertIn("Proposed cleanup for mixed", items["mixed"]["reason"])
        self.assertIsNone(items["mixed"]["archive_reason"])
        self.assertEqual(items["appearance"]["action"], "review")
        self.assertEqual(items["wave"]["archive_reason"], "low_value")

        receipt = self.service.apply(preview["preview_id"])
        self.assertEqual((receipt["archived"], receipt["reviewed"]), (1, 2))
        self.assertTrue(self.store.get_by_id("mixed").is_active())
        self.assertEqual(self.store.get_by_id("mixed").review_note, items["mixed"]["reason"])
        reopened = MemoryCuration("fixture", LongTermStore(self.root))
        self.assertEqual(reopened.apply(preview["preview_id"]), receipt)
        wave = reopened.store.get_by_id("wave")
        reopened.restore(wave.id, wave.archive_revision)
        self.assertTrue(reopened.store.get_by_id("wave").is_active())
        with self.assertRaises(CurationConflict):
            reopened.restore(wave.id, wave.archive_revision)

    async def test_missing_provenance_does_not_prove_unsupported(self):
        self.seed(note("claim", "Maybe the user has a medical condition.", category="ai_insight"))
        await self.service.run_daily("2026-11-07", propose_archive("unsupported"))
        fact = self.store.get_by_id("claim")
        self.assertTrue(fact.is_active())
        self.assertTrue(fact.review_note)

    async def test_cold_archive_checks_kind_source_role_date_and_active_status(self):
        source = {"source": "single", "id": 3, "timestamp": 1793923200.0,
                  "speaker_role": "user", "assertion": "self_statement"}
        assistant = {**source, "id": 4, "speaker_role": "assistant", "assertion": "character_context"}
        self.seed(
            note("user", "The user described learning to fold paper as a child.",
                 kind="experience", evidence=[source]),
            note("shared", "We folded a paper crane together.", kind="experience",
                 occurred_from="2024-04-17", occurred_to="2024-04-18", evidence=[source, assistant]),
            note("assistant-only", "We learned to fold paper.", kind="experience", evidence=[assistant]),
            note("wrong-role", "Another shared experience.", kind="experience",
                 evidence=[{**source, "speaker_role": "tool"}]),
            note("no-role", "One shared experience.", kind="experience",
                 evidence=[{key: value for key, value in source.items() if key != "speaker_role"}]),
            note("wrong-assertion", "A shared experience.", kind="experience",
                 evidence=[source, {**assistant, "assertion": "self_statement"}]),
            note("no-source", "A shared experience.", kind="experience",
                 evidence=[{**source, "source": ""}]),
            note("bad-time", "Some shared experience.", kind="experience",
                 evidence=[{**source, "timestamp": "unknown"}]),
            note("no-evidence", "A childhood experience.", kind="experience"),
            note("reversed", "A shared experience.", kind="experience", evidence=[source],
                 occurred_from="2024-04-18", occurred_to="2024-04-17"),
            note("future", "We discussed the recital.", kind="experience", evidence=[source],
                 occurred_from="2099-11-09"),
            note("floating", "We folded paper last Tuesday.", kind="experience", evidence=[source]),
            note("dated-plan", "We discussed a future recital.", kind="experience", evidence=[source],
                 event_date="2099-11-09"),
            note("legacy", "We shared an experience.", evidence=[source]),
            note("stable", "I wear glasses.", kind="stable", evidence=[source]),
            note("planned", "We plan to fold paper.", kind="event", event_date="2099-11-09"),
            note("temporary", "I am at the studio.", kind="temporary", expires_at=time.time() + 600),
            note("expired", "The event was scheduled.", kind="event", expires_at=time.time() - 1),
            note("address", "Preferred name is Kai.", kind="address", evidence=[source]),
            note("pinned", "Another childhood experience.", kind="experience", evidence=[source], pinned=True),
        )
        model = propose_archive("cold_memory")
        await self.service.run_daily("2026-11-07", model)
        for fact_id in ("user", "shared"):
            fact = self.store.get_by_id(fact_id)
            self.assertTrue(fact.archived, fact_id)
            self.assertEqual(fact.archive_reason, "cold_memory")
            self.assertTrue(fact.evidence)
        for fact_id in ("assistant-only", "wrong-role", "no-role", "wrong-assertion", "no-source",
                        "bad-time", "no-evidence", "reversed", "future", "floating", "dated-plan",
                        "legacy", "stable", "planned", "temporary"):
            fact = self.store.get_by_id(fact_id)
            self.assertTrue(fact.is_active(), fact_id)
            self.assertTrue(fact.review_note, fact_id)
        for fact_id in ("expired", "address", "pinned"):
            fact = self.store.get_by_id(fact_id)
            self.assertFalse(fact.archived)
            self.assertFalse(fact.review_note)

    async def test_extraction_evidence_survives_daily_cold_archive_and_restore(self):
        revision, cursors, facts = self.store.extraction_snapshot()
        user = {"source": "single", "id": 1, "timestamp": 1793923200.0, "role": "user",
                "content": "We folded a paper crane on 2024-04-17."}
        assistant = {"source": "single", "id": 2, "timestamp": 1793923300.0, "role": "assistant",
                     "generation_status": "completed", "content": "I remember the paper crane."}
        rows = {"single:1": user, "single:2": assistant}
        apply_operations(facts, [{
            "op": "add", "kind": "experience", "category": "milestone",
            "content": "We folded a paper crane together.", "occurred_from": "2024-04-17",
            "retention_reason": "We learned together.", "evidence": list(rows),
        }], rows, facts, 0.5)
        self.assertTrue(self.store.apply_extraction(revision, facts, cursors))
        fact, = facts
        self.assertIn(fact.id, RecallCandidates(self.store).active)
        await self.service.run_daily("2026-11-07", propose_archive("cold_memory"))
        reopened = LongTermStore(self.root)
        recall = RecallCandidates(reopened)
        self.assertNotIn(fact.id, recall.active)
        archived = recall.cold[fact.id]
        self.assertEqual((archived.content, archived.evidence, archived.occurred_from),
                         (fact.content, fact.evidence, fact.occurred_from))
        self.assertEqual(archived.retention_reason, fact.retention_reason)
        MemoryCuration("fixture", reopened).restore(fact.id, archived.archive_revision)
        self.assertIn(fact.id, RecallCandidates(reopened).active)

    async def test_bulk_preview_covers_every_id_without_touching_pending_apply_or_store(self):
        self.seed(
            note("a", "Waved once"),
            *(note(f"b{i:02d}", f"Note {i}") for i in range(27)),
            note("mixed", "I waved once. The user corrected me: I wear glasses in real life."),
            note("pinned", "A pinned note.", pinned=True),
            note("saved", "An archive.", archived=True),
            note("stale", "A past plan.", expires_at=time.time() - 1),
        )
        model = propose_archive("low_value")
        preview = await self.service.preview(model)
        path = Path(self.root, "long_term_facts.json")
        original = path.read_bytes()
        page = await self.service.preview_bulk(model)
        self.assertEqual(page["profile_id"], "fixture")
        self.assertEqual(page["coverage"], {"total": 32, "offset": 0, "covered": 24})
        self.assertNotIn("preview_id", page)
        cursor = page["next_cursor"]
        self.assertTrue(cursor)
        final = await self.service.preview_bulk(model, cursor=cursor)
        self.assertEqual(final["coverage"], {"total": 32, "offset": 24, "covered": 32})
        self.assertIsNone(final["next_cursor"])
        items = [*page["items"], *final["items"]]
        self.assertEqual([item["fact_id"] for item in items], sorted(fact.id for fact in self.store.get_all()))
        actions = {item["fact_id"]: item["action"] for item in items}
        self.assertEqual(actions["mixed"], "review")
        for protected in ("pinned", "saved", "stale"):
            self.assertEqual(actions[protected], "keep")
            self.assertNotIn(protected, {fact.id for fact in model.await_args.args[0]})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.service.apply(preview["preview_id"], ["a"])["archived"], 1)

    async def test_bulk_preview_rejects_a_continuation_after_a_store_change(self):
        self.seed(*(note(str(i), f"Note {i}") for i in range(30)))
        model = propose_archive("low_value")
        page = await self.service.preview_bulk(model)
        LongTermStore(self.root).update("29", pinned=True)
        with self.assertRaises(CurationConflict):
            await self.service.preview_bulk(model, cursor=page["next_cursor"])
        model.assert_awaited_once()
