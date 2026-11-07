import json
from unittest import TestCase

from src.memory.batch_curation import (
    make_review_batch, review_batch_messages, select_review_subjects, validate_batch_review,
)
from src.memory.long_term_store import LongTermFact


def fact(id, content="The character waved once.", age=1, **kwargs):
    return LongTermFact(id=id, content=content, source="auto", is_manual=False, updated_at=age, **kwargs)


def answer(*actions):
    return {"items": [
        {"id":i, "action":action, "reason":"Review reason"}
        for i,action in enumerate(actions, 1)
    ]}


class CapacityReviewTests(TestCase):
    def test_cap_selects_oldest_thirty_and_excludes_archives_and_protected(self):
        rows = [fact(f"f{i}", age=i) for i in range(45, 0, -1)]
        rows += [fact("protected", pinned=True), fact("archived", archived=True)]
        subjects, active = select_review_subjects(rows, {}, 20)
        self.assertEqual(active, 46)
        self.assertEqual([row.id for row in subjects], [f"f{i}" for i in range(1,31)])

    def test_repeat_evidence_weight_and_clock_do_not_requeue_but_content_does(self):
        row = fact("f1", "The character waved once.")
        batch = make_review_batch([row], [row], 1, 500)
        _, reviewed = validate_batch_review(batch, answer("keep"))
        row.updated_at += 10
        row.weight = 0.2
        row.evidence = [{"speaker_role":"assistant"}]
        self.assertEqual(select_review_subjects([row], reviewed, 500)[0], [])
        row.content = "The character nodded once."
        self.assertEqual(select_review_subjects([row], reviewed, 500)[0], [row])

    def test_cross_batch_exact_target_and_capacity_in_request(self):
        first = fact("a", "The user prefers unsweetened tea.", kind="stable", category="preference")
        outside = fact("b", "The user prefers unsweetened tea.", kind="stable", category="preference")
        batch = make_review_batch([first], [first, outside], 2, 1, comparison=lambda *_: outside)
        payload = json.loads(review_batch_messages(batch, review_prompt="Judge utility.")[-1]["content"])
        self.assertEqual(payload["capacity"], {"active":2,"limit":1,"excess":1})
        self.assertEqual(payload["records"][0]["comparison"]["content"], outside.content)
        decisions, _ = validate_batch_review(batch, answer("merge"))
        self.assertEqual(decisions[0]["target_id"], outside.id)

    def test_near_match_cannot_hide_independent_information(self):
        source = fact("a", "The user is allergic to peanuts and sesame.", kind="stable", category="preference")
        target = fact("b", "The user is allergic to peanuts.", kind="stable", category="preference")
        batch = make_review_batch([source], [source, target], 2, 1, comparison=lambda *_: target)
        self.assertEqual(validate_batch_review(batch, answer("merge"))[0][0]["action"], "keep")

    def test_existing_merge_representative_must_stay_active(self):
        source = fact("a", "The user prefers unsweetened tea.", kind="stable", category="preference")
        target = fact("b", source.content, kind="stable", category="preference")
        archived = fact("old", source.content, archived=True, archive_reason="merged", merged_into=source.id)
        batch = make_review_batch([source], [source, target, archived], 2, 1, comparison=lambda *_: target)
        self.assertEqual(validate_batch_review(batch, answer("merge"))[0][0]["action"], "keep")

    def test_archive_requires_positive_evidence_in_legacy_text(self):
        action = answer("archive")
        real = fact("a", "My sister lives in Toronto.", category="relationship")
        question = fact("b", "角色观察到用户询问IPv4和IPv6。关键词：IPv4 IPv6")
        for row, expected in ((real, "keep"), (question, "archive")):
            with self.subTest(row=row.id):
                batch = make_review_batch([row], [row], 1, 1)
                self.assertEqual(validate_batch_review(batch, action)[0][0]["action"], expected)

    def test_capacity_never_forces_an_archive_or_rebills_same_keep(self):
        first = fact("a")
        protected = fact("b", "The user has a passport.", pinned=True)
        batch = make_review_batch([first], [first, protected], 2, 1)
        actions, reviewed = validate_batch_review(batch, answer("keep"))
        self.assertEqual(actions[0]["action"], "keep")
        self.assertEqual(select_review_subjects([first,protected], reviewed, 1)[0], [])

    def test_archiving_named_real_life_facts_and_conflicting_ids_are_blocked(self):
        row = fact("a", "角色得知用户喷了名为 Harbor Mist 的香水。")
        batch = make_review_batch([row], [row], 1, 1)
        decisions, _ = validate_batch_review(batch, answer("archive"))
        self.assertEqual(decisions[0]["action"], "keep")
        with self.assertRaises(ValueError):
            validate_batch_review(batch, {"items":[*answer("archive")["items"], *answer("keep")["items"]]})
