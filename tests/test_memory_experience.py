import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from src.memory.fact_operations import apply_operations, choose_candidates, rank_related
from src.memory.long_term_store import LongTermFact, LongTermStore


def row(message_id, content, role="user", **fields):
    result = {"source": "single", "id": message_id, "timestamp": 1794000000,
              "content": content, "role": role, **fields}
    ref = f"single:{message_id}"
    if "start_char" in fields:
        ref += f"#{fields['start_char']}-{fields['end_char']}"
    return ref, dict(result, reference=ref)


def experience(content="That evening we talked about the recital.", **fields):
    return {"op": "add", "kind": "experience", "content": content,
            "category": "relationship", "retention_reason": "The change in our conversation mattered.",
            **fields}


def apply(facts, proposal, *rows):
    return apply_operations(facts, [{**proposal, "evidence": [ref for ref, _ in rows]}],
                            dict(rows), facts, 0.5)


class ExperienceTests(TestCase):
    def test_three_updates_keep_one_identity_and_complete_previous_versions(self):
        root = self.enterContext(TemporaryDirectory())
        store = LongTermStore(root)
        revision, _, facts = store.extraction_snapshot()
        apply(facts, experience(occurred_from="2024-04-17", emotional_note="I felt relieved"),
              row(1, "We talked after my recital on 2024-04-17."),
              row(2, "I can listen.", "assistant", generation_status="completed"))
        self.assertEqual(len(facts), 1)
        fact_id = facts[0].id
        for version in range(1, 4):
            apply(facts, {"op": "update", "fact_id": fact_id, "expected_version": version,
                          "content": f"After the recital I listened. They shared detail {version}."},
                  row(version + 2, f"To continue the recital story, here is detail {version}."))
        self.assertTrue(store.apply_extraction(revision, facts, {"single": 5}))

        fact, = LongTermStore(root).get_active(now=10**12)
        self.assertEqual((fact.id, fact.record_version, fact.kind), (fact_id, 4, "experience"))
        self.assertEqual((fact.occurred_from, fact.occurred_to, fact.expires_at), ("2024-04-17", None, None))
        self.assertEqual(fact.emotional_note, "I felt relieved")
        self.assertEqual([entry["id"] for entry in fact.evidence], [1, 2, 3, 4, 5])
        self.assertEqual([(entry["speaker_role"], entry["assertion"]) for entry in fact.evidence[:2]],
                         [("user", "self_statement"), ("assistant", "character_context")])
        self.assertEqual([entry["record_version"] for entry in fact.versions], [1, 2, 3])
        for previous in fact.versions:
            self.assertEqual(previous["occurred_from"], fact.occurred_from)
            self.assertEqual(previous["retention_reason"], fact.retention_reason)
            self.assertNotIn("versions", previous)

    def test_undated_historical_text_and_length_limit(self):
        facts = []
        body = "That evening we discussed the recital. " + "x" * 1161
        self.assertEqual(len(body), 1200)
        apply(facts, experience(body), row(1, "We talked about the recital a while ago."))
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].content, body)
        self.assertIsNone(facts[0].occurred_from)
        self.assertIsNone(facts[0].occurred_to)
        for content in (body + "x", "We plan to discuss the recital next Monday."):
            with self.subTest(content=content[-50:]):
                with self.assertRaises(ValueError):
                    apply(facts, experience(content), row(2, content))
        self.assertEqual(len(facts), 1)

    def test_only_user_evidence_can_support_an_experience_or_its_dates(self):
        facts = []
        user = row(1, "I remember talking after the recital.")
        assistant = row(2, "We talked on 2024-04-17.", "assistant")
        partial = row(3, "I listened.", "assistant", generation_status="partial")
        for proposal, evidence in (
            (experience(), (assistant,)),
            (experience(), (user, partial)),
            (experience(occurred_from="2024-04-17"), (user, assistant)),
            (experience(occurred_from="2026-11-07"), (user,)),
            (experience("We talked on 2024-04-17."), (user,)),
            (experience(occurred_from="2024-04-18", occurred_to="2024-04-17"),
             (row(1, "We talked on 2024-04-17 and 2024-04-18."),)),
            (experience(occurred_from="2024-02-30"), (row(1, "I said 2024-02-30."),)),
            (experience(occurred_from="2099-01-01"), (row(1, "I plan to go on 2099-01-01."),)),
            ({"op": "add", "kind": "stable", "content": "They enjoy recitals."}, (user, assistant)),
        ):
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                apply(facts, proposal, *evidence)
        self.assertEqual(facts, [])

    def test_confirm_and_update_do_not_reassign_an_established_date(self):
        facts = []
        first = row(1, "I remember our recital conversation on 2024-04-17.")
        other = row(2, "We had another recital conversation on 2024-05-10.")
        apply(facts, experience(occurred_from="2024-04-17"), first)
        apply(facts, experience(occurred_from="2024-05-10"), other)
        self.assertEqual(len(facts), 2)
        first_fact = facts[0]
        for action in ("confirm", "update"):
            proposal = experience(op=action, fact_id=first_fact.id, expected_version=1,
                                  occurred_from="2024-05-10")
            with self.subTest(action=action), self.assertRaises(ValueError):
                apply(facts, proposal, other)
        wrong_date = row(3, "Our recital conversation on 2024-05-10 mattered to me.")
        for action in ("confirm", "update"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                apply(facts, {"op": action, "fact_id": first_fact.id, "expected_version": 1,
                              "content": "The conversation mattered to them."}, wrong_date)
        with self.assertRaises(ValueError):
            apply(facts, {"op": "confirm", "fact_id": first_fact.id, "expected_version": 2}, first)
        apply(facts, {"op": "confirm", "fact_id": first_fact.id, "expected_version": 1}, first)
        self.assertEqual(first_fact.record_version, 1)
        self.assertEqual([entry["id"] for entry in first_fact.evidence], [1])

    def test_same_date_and_text_require_shared_user_evidence_to_deduplicate(self):
        facts = []
        first = row(1, "We talked after the recital on 2024-04-17.")
        second = row(2, "We had a separate talk about the recital on 2024-04-17.")
        assistant = row(3, "I'm here to listen.", "assistant")
        proposal = experience(occurred_from="2024-04-17")
        apply(facts, proposal, first, assistant)
        apply(facts, proposal, second, assistant)
        self.assertEqual(len(facts), 2)
        self.assertEqual([entry["id"] for entry in facts[0].evidence], [1, 3])
        self.assertEqual([entry["id"] for entry in facts[1].evidence], [2, 3])

        self.assertEqual(apply(facts, proposal, first, assistant), set())
        self.assertEqual(len(facts), 2)
        apply(facts, {"op": "confirm", "fact_id": facts[0].id},
              row(4, "I remember the first recital conversation."))
        self.assertEqual([entry["id"] for entry in facts[0].evidence], [1, 3, 4])
        self.assertEqual(facts[0].record_version, 1)

    def test_tags_only_experience_update_creates_version_and_requests_reindex(self):
        facts = []
        proposal = experience(keywords=["recital"])
        apply(facts, proposal, row(1, "We talked about the recital."))
        fact, = facts
        update = {"op": "update", "fact_id": fact.id, "content": fact.content,
                  "retention_reason": fact.retention_reason, "keywords": ["recital", "stage-fright"]}
        changed = apply(facts, update, row(2, "The recital conversation was about stage fright."))

        self.assertEqual(changed, {fact.id})
        self.assertEqual(fact.tags, ["recital", "stage-fright"])
        self.assertEqual(fact.record_version, 2)
        self.assertEqual(fact.versions[0]["tags"], ["recital"])
        self.assertEqual(apply(facts, update, row(2, "The recital conversation was about stage fright.")), set())
        self.assertEqual(fact.record_version, 2)

    def test_distinct_slices_of_the_same_message_keep_their_ranges(self):
        facts = []
        first = "We talked on 2024-04-17."
        second = "They asked for a quiet moment."
        digest = hashlib.sha256((first + second).encode()).hexdigest()
        source = {"content_hash": digest}
        first_slice = row(1, first, start_char=0, end_char=len(first), **source)
        second_slice = row(1, second, start_char=len(first), end_char=len(first + second), **source)
        apply(facts, experience(occurred_from="2024-04-17"), first_slice)
        confirm = {"op": "confirm", "fact_id": facts[0].id}
        apply(facts, confirm, second_slice)
        apply(facts, confirm, second_slice)
        self.assertEqual([(item["start_char"], item["end_char"], item["content_hash"])
                          for item in facts[0].evidence],
                         [(0, len(first), digest), (len(first), len(first + second), digest)])
        with self.assertRaises(ValueError):
            apply(facts, confirm, ("single:99", first_slice[1]))

    def test_lexical_recall_requires_overlap_beyond_english_function_words(self):
        experience = LongTermFact(content="We were rehearsing for a recital.", kind="experience",
                                  archived=True, archive_reason="cold_memory")
        events = [
            LongTermFact(content="I was scheduled to visit the studio.", kind="event"),
            LongTermFact(content="I booked a meeting with the architect.", kind="event"),
        ]
        dinner = LongTermFact(content="Dinner favorites include lentil soup.", kind="stable")
        facts = [experience, *events, dinner]
        self.assertEqual(rank_related(facts, "What should I make for dinner?", 4), [dinner])
        self.assertEqual(rank_related(facts, "How was the recital?", 4), [experience])

    def test_cold_candidate_matches_late_content_and_stays_archived(self):
        cold = LongTermFact(content="The rehearsal felt overwhelming.", kind="experience",
                            is_manual=False, source="auto", archived=True, archive_reason="cold_memory",
                            archive_revision="previous-archive")
        other = LongTermFact(content="Pottery and ceramics.")
        late = row(2, "x" * 1100 + " During rehearsal I asked for a break.")[1]
        candidates = choose_candidates([cold, other], [late])
        self.assertEqual(candidates, [cold])
        self.assertEqual(cold.evidence, [])
        apply([cold], {"op": "update", "fact_id": cold.id,
                       "content": "During rehearsal they asked for a break."},
              (late["reference"], late))
        self.assertTrue(cold.archived)
        self.assertEqual(cold.archive_reason, "cold_memory")
        self.assertEqual(cold.record_version, 2)
        self.assertNotEqual(cold.archive_revision, "previous-archive")
        self.assertEqual(cold.versions[0]["archive_revision"], "previous-archive")

    def test_experience_updates_share_the_candidate_version_at_batch_start(self):
        facts = []
        apply(facts, experience(), row(1, "I remember our recital conversation."))
        first = row(2, "I remember asking for a pause.")
        second = row(3, "I remember wanting to keep talking.")
        updates = [{"op": "update", "fact_id": facts[0].id, "evidence": [ref],
                    "content": item["content"]} for ref, item in (first, second)]
        with self.assertRaises(ValueError):
            apply_operations(facts, updates, dict((first, second)), facts, 0.5)


class ExtractionOffsetTests(TestCase):
    def test_offsets_persist_through_legacy_writes_and_stale_commit(self):
        root = self.enterContext(TemporaryDirectory())
        path = Path(root) / "long_term_facts.json"
        path.write_text(json.dumps({"facts": [{"id": "legacy", "content": "Legacy content.",
                                               "evidence": [{"source": "single", "id": 2}]}]}), encoding="utf-8")
        store = LongTermStore(root)
        rev, cursors, facts, offsets = store.extraction_progress_snapshot()
        self.assertEqual((cursors, offsets), ({}, {}))
        self.assertEqual(facts[0].evidence, [{"source": "single", "id": 2}])
        self.assertEqual((facts[0].occurred_from, facts[0].occurred_to, facts[0].retention_reason),
                         (None, None, ""))
        offsets["single"] = {"id": 3, "offset": 480, "content_hash": "digest"}
        self.assertTrue(store.apply_extraction_progress(rev, facts, {"single": 2}, offsets))
        offsets["single"]["offset"] = 9
        self.assertFalse(store.apply_extraction_progress(rev, [], {"single": 3}, {}))
        self.assertTrue(store.update("legacy", pinned=True))
        rev, cursors, facts, state = store.curation_snapshot()
        self.assertTrue(store.apply_curation(rev, facts, cursors, state))
        rev, cursors, facts = store.extraction_snapshot()
        self.assertTrue(store.apply_extraction(rev, facts, cursors))

        reloaded = LongTermStore(root)
        rev, cursors, facts, offsets = reloaded.extraction_progress_snapshot()
        expected = {"single": {"id": 3, "offset": 480, "content_hash": "digest"}}
        self.assertEqual((cursors, offsets), ({"single": 2}, expected))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["source_offsets"], expected)
        self.assertTrue(facts[0].pinned)
        self.assertTrue(reloaded.apply_extraction_progress(rev, facts, {"single": 3}, {}))
        self.assertNotIn("source_offsets", json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(LongTermStore(root).extraction_progress_snapshot()[3], {})

    def test_failed_write_keeps_facts_cursors_and_offsets(self):
        root = self.enterContext(TemporaryDirectory())
        store = LongTermStore(root)
        revision, _, facts, _ = store.extraction_progress_snapshot()
        facts.append(LongTermFact(content="This fact should not be committed."))
        with patch("src.memory.long_term_store.os.replace", side_effect=OSError("fixture")):
            self.assertFalse(store.apply_extraction_progress(
                revision, facts, {"single": 3}, {"single": {"id": 4, "offset": 80, "content_hash": "digest"}},
            ))
        self.assertEqual(store.extraction_progress_snapshot(), (revision, {}, [], {}))
        self.assertEqual(LongTermStore(root).extraction_progress_snapshot()[1:], ({}, [], {}))
