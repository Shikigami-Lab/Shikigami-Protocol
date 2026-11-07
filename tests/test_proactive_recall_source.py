import asyncio
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.commands.base import dispatch_command
from src.core.session import Session
from src.core.topics import get_source
from src.core.topics.base import TopicSourceContext
from src.memory.conversation_store import Message
from src.memory.extraction_batches import message_hash
from src.memory.long_term_store import LongTermFact, LongTermStore


class ProactiveRecallSourceTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.session = Session("fixture", "fixture", "Fixture", str(self.root))
        self.store = self.session.conversation_store
        self.addCleanup(self.store.close)
        self.app = SimpleNamespace(state=SimpleNamespace())
        self.ctx = TopicSourceContext("fixture", str(self.root), session=self.session, app=self.app)
        self.source = get_source("conversation_recall")
        self.today = date.today()

    def message(self, days_ago, content, *, role="user", status="completed", message_id=None):
        day = self.today - timedelta(days=days_ago)
        return Message(role, content, datetime.combine(day, time(12)).timestamp(),
                       generation_status=status, id=message_id)

    def experience(self, row, **kwargs):
        ref = {"source": "single", "id": row.id, "speaker_role": "user", "assertion": "self_statement",
               "timestamp": row.timestamp, "content_hash": message_hash(row.content)}
        return LongTermFact(kind="experience", **{
            "content": "The user had an experience at the museum.", "evidence": [ref], **kwargs,
        })

    def save_facts(self, *facts):
        store = LongTermStore(str(self.root))
        revision, cursors, _ = store.extraction_snapshot()
        self.assertTrue(store.apply_extraction(revision, list(facts), cursors))
        self.ctx.memory_manager = SimpleNamespace(facts=store)

    def test_reads_original_user_rows_from_recent_and_older_days(self):
        recent = "I finished restoring my grandfather's radio."
        older = "我参加了社区的陶艺展。"
        self.store.save_all([
            self.message(4, recent, message_id=41),
            self.message(32, older, message_id=12),
            self.message(4, "I won the dragon race.", role="assistant"),
            self.message(32, "I completed a different project.", role="system"),
            self.message(10, "I attended the exhibition.", status="failed"),
        ])

        candidates = self.source.get_candidates(self.ctx)
        self.assertEqual(len(candidates), 2)
        for candidate, days_ago, message_id, original in zip(
            candidates, (4, 32), (41, 12), (recent, older),
        ):
            with self.subTest(original=original):
                day = (self.today - timedelta(days=days_ago)).isoformat()
                self.assertEqual(candidate.source_id, "conversation_recall")
                self.assertIn(day, candidate.summary)
                self.assertIn(original, candidate.summary)
                material = get_source(candidate.source_id).resolve(candidate.item_id, self.ctx)
                self.assertEqual(material.item_id, candidate.item_id)
                self.assertEqual(material.meta["message_id"], message_id)
                self.assertEqual(material.meta["date"], day)
                self.assertIn(original, material.material)
        self.assertEqual(self.source.get_candidates(self.ctx), candidates)

    def test_rejects_plans_progress_countdowns_and_ambiguous_claims(self):
        rejected = (
            "I plan to finish restoring the radio.",
            "I finished the poster and will submit it tomorrow.",
            "I finished 3 out of 10 chapters.",
            "I completed 80% of the project.",
            "I finished half of the project.",
            "I finished packing. Only 3 days until the trip.",
            "I finished packing. Two days left.",
            "I finished planning the trip.",
            "I finished the first draft of the report.",
            "I finished the assignment?",
            "I visited the palace in a dream.",
            "I love cooking.",
            "我打算明天去逛博物馆。",
            "我完成了海报，明天准备去投稿。",
            "我完成了项目的一部分，还有两天交付。",
            "我完成了三分之一的进度。",
            "我看完了第一章，离考试还剩三天。",
            "我完成了海报，三天后去投稿。",
        )
        self.store.save_all([self.message(2 + i, content) for i, content in enumerate(rejected)])
        self.assertEqual(self.source.get_candidates(self.ctx), [])

    def test_bounds_age_and_diversifies_busy_days(self):
        self.store.save_all([
            self.message(day, f"I visited museum number {day}.")
            for day in (0, 1, 3, 4, 5, 6, 7, 8, 15, 45, 180, 181)
        ] + [self.message(2, f"I watched film number {i}.") for i in range(100)])

        candidates = self.source.get_candidates(self.ctx)
        ages = [
            (self.today - date.fromisoformat(self.source.resolve(c.item_id, self.ctx).meta["date"])).days
            for c in candidates
        ]
        self.assertEqual(ages, [2, 15, 3, 45, 4, 180])

    def test_ignores_out_of_range_and_only_scans_a_bounded_sample_per_day(self):
        self.store.save_all([
            self.message(181, "I visited the museum."),
            self.message(1, "I visited the railway station."),
            self.message(5, "I finished restoring the radio."),
            *[self.message(5, "I am going to the park.") for _ in range(16)],
        ])
        self.assertEqual(self.source.get_candidates(self.ctx), [])

    def test_recently_used_reference_is_excluded(self):
        self.store.save_all([self.message(4, "I finished restoring the radio.")])
        candidate, = self.source.get_candidates(self.ctx)
        self.ctx.recently_used.add(candidate.ref)
        self.assertEqual(self.source.get_candidates(self.ctx), [])
        self.assertIsNone(self.source.resolve(candidate.item_id, self.ctx))

    def test_resolve_revalidates_original_even_if_same_id_and_day_survive(self):
        content = "I completed the pottery workshop."
        self.store.save_all([self.message(20, content, message_id=22)])
        candidate, = self.source.get_candidates(self.ctx)
        self.assertIsNone(self.source.resolve((self.today - timedelta(days=20)).isoformat(), self.ctx))
        for row in (
            self.message(20, content, role="assistant", message_id=22),
            self.message(20, "I completed the coding workshop.", message_id=22),
            self.message(21, content, message_id=22),
        ):
            self.store.save_all([row])
            self.assertIsNone(self.source.resolve(candidate.item_id, self.ctx))
        self.store.delete_by_id(22)
        self.assertIsNone(self.source.resolve(candidate.item_id, self.ctx))
        self.assertEqual(self.source.get_candidates(self.ctx), [])

    def test_missing_store_does_not_create_or_migrate_profile_files(self):
        missing_root = self.root / "other"
        session = Session("other", "other", "Other", str(missing_root))
        ctx = TopicSourceContext("other", str(missing_root), session=session)
        self.assertEqual(self.source.get_candidates(ctx), [])
        self.assertIsNone(self.source.resolve("2026-10-30.1.1111111111111111", ctx))
        self.assertFalse(missing_root.exists())
        self.store.close()
        before = set(self.root.iterdir())
        self.assertEqual(self.source.get_candidates(self.ctx), [])
        self.assertEqual(set(self.root.iterdir()), before)

    def test_context_must_match_single_chat_profile(self):
        self.store.save_all([self.message(3, "I finished restoring the radio.")])
        candidate, = self.source.get_candidates(self.ctx)
        self.ctx.profile_id = "other"
        self.assertEqual(self.source.get_candidates(self.ctx), [])
        self.assertIsNone(self.source.resolve(candidate.item_id, self.ctx))

    def test_topic_recall_command_uses_same_source(self):
        original = "I visited the railway museum."
        self.store.save_all([self.message(18, original)])
        result = asyncio.run(dispatch_command("/topic recall", self.session, self.app))
        self.assertIn(original, result.context_for_llm)

    def test_prioritizes_two_linked_experiences_and_deduplicates_lexical_original(self):
        first = self.message(50, "I visited the railway museum.", message_id=1)
        second = self.message(70, "At the museum, we spent hours restoring the signal box.", message_id=2)
        third = self.message(80, "At the museum, we spent the afternoon with the curator.", message_id=3)
        fallback = self.message(5, "I attended the ceramics workshop.", message_id=4)
        self.store.save_all([first, second, third, fallback])
        lexical = next(c for c in self.source.get_candidates(self.ctx) if first.content in c.summary)
        self.save_facts(self.experience(first, updated_at=3), self.experience(second, updated_at=2),
                        self.experience(third, updated_at=1))

        candidates = self.source.get_candidates(self.ctx)
        materials = [self.source.resolve(candidate.item_id, self.ctx) for candidate in candidates]
        self.assertEqual([m.meta["message_id"] for m in materials], [1, 2, 4])
        self.assertIn(second.content, materials[1].material)
        self.assertNotIn("The user had an experience", materials[1].material)

        self.ctx.recently_used = {candidates[0].ref}
        self.assertIsNone(self.source.resolve(lexical.item_id, self.ctx))
        self.ctx.recently_used = {lexical.ref}
        self.assertIsNone(self.source.resolve(candidates[0].item_id, self.ctx))
        self.assertFalse(any(first.content in c.summary for c in self.source.get_candidates(self.ctx)))

    def test_rechecks_active_fact_and_exact_original_at_resolve(self):
        row = self.message(40, "I visited the railway museum.", message_id=1)
        self.store.save_all([row])
        fact = self.experience(row)
        self.save_facts(fact)
        candidate, = self.source.get_candidates(self.ctx)
        self.assertIsNotNone(self.source.resolve(candidate.item_id, self.ctx))
        fact.archived = True
        self.save_facts(fact)
        self.assertIsNone(self.source.resolve(candidate.item_id, self.ctx))
        fact.archived = False
        original_fact_text = fact.content
        fact.content = "The user went to the museum with a friend."
        self.save_facts(fact)
        self.assertIsNone(self.source.resolve(candidate.item_id, self.ctx))
        fact.content = original_fact_text
        self.save_facts(fact)
        self.store.save_all([self.message(40, "I visited a different museum.", message_id=1)])
        self.assertIsNone(self.source.resolve(candidate.item_id, self.ctx))
        self.store.delete_by_id(1)
        self.assertEqual(self.source.get_candidates(self.ctx), [])

    def test_experiences_require_safe_user_evidence_and_a_past_event(self):
        cases = [
            ("We spent the day restoring a signal box.", {}, {"source": "group:room"}, "user"),
            ("We spent the day restoring a signal box.", {}, {}, "assistant"),
            ("We spent the day restoring a signal box.", {}, {"content_hash": ""}, "user"),
            ("We spent the day restoring a signal box.", {}, {"content_hash": "0" * 64}, "user"),
            ("We spent the day restoring a signal box.", {}, {"start_char": 3, "end_char": 3}, "user"),
            ("We spent the day restoring a signal box.", {"archived": True}, {}, "user"),
            ("We spent the day restoring a signal box.", {"content": "Plans to visit the railway museum."}, {}, "user"),
            ("We spent the day restoring a signal box.", {"occurred_to": "2099-01-01"}, {}, "user"),
            ("I'm considering visiting the railway museum.", {}, {}, "user"),
            ("We're visiting the railway museum on Saturday.", {}, {}, "user"),
            ("I haven't visited the railway museum.", {}, {}, "user"),
            ("We spent the day restoring a signal box in a dream.", {}, {}, "user"),
            ("I imagined we spent the day restoring a signal box.", {}, {}, "user"),
        ]
        for i, (content, fields, ref_overrides, role) in enumerate(cases, start=1):
            with self.subTest(i=i, content=content):
                row = self.message(35, content, role=role, message_id=i)
                self.store.save_all([row])
                fact = self.experience(row, **fields)
                fact.evidence[0].update(ref_overrides)
                self.save_facts(fact)
                self.assertEqual(self.source.get_candidates(self.ctx), [])

    def test_uses_only_verified_span_and_rechecks_its_link(self):
        quote = "We spent the afternoon talking with the curator."
        content = "Museum visit notes. " + quote
        row = self.message(34, content, message_id=1)
        self.store.save_all([row])
        fact = self.experience(row)
        fact.evidence[0].update(start_char=content.index(quote), end_char=len(content))
        self.save_facts(fact)
        candidate, = self.source.get_candidates(self.ctx)
        material = self.source.resolve(candidate.item_id, self.ctx)
        self.assertIn(quote, material.material)
        self.assertNotIn("Museum visit notes.", material.material)
        self.app.state.memory_managers = {self.session.profile_id: self.ctx.memory_manager}
        command = asyncio.run(dispatch_command("/topic recall", self.session, self.app))
        self.assertIn(quote, command.context_for_llm)
        fact.evidence[0]["start_char"] += 3
        self.save_facts(fact)
        self.assertIsNone(self.source.resolve(candidate.item_id, self.ctx))
