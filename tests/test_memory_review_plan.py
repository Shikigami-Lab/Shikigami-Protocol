"""Tests for one fixed, source-only memory review work item."""
from copy import deepcopy
import json
from unittest import TestCase
from unittest.mock import patch

from src.memory import curation_plan as review
from src.memory.long_term_store import LongTermFact


def fact(content, id="source", **fields):
    return LongTermFact(content, id=id, **{"source": "auto", "is_manual": False, **fields})


def proposal(subject, action="archive", **fields):
    item = {"fact_id": subject.id, "action": action, "reason": "The note has no lasting use.",
            "future_use": "", "evidence_quote": subject.content}
    if action == "archive":
        item["archive_reason"] = "ephemeral"
    return {**item, **fields}


class FixedReviewTests(TestCase):
    def decision(self, subject, action="archive", comparison=None, **fields):
        rows = [subject] if comparison is None else [subject, comparison]
        result, = review.validate_review(rows, [proposal(subject, action, **fields)])
        self.assertEqual(result["fact_id"], subject.id)
        self.assertEqual(result["content"], subject.content)
        self.assertIsInstance(result["reason"], str)
        return result

    def test_messages_identify_only_one_subject_and_fixed_comparison(self):
        subject = fact("The curtain fell after the scene.")
        comparison = fact("The lantern went out after the scene.", id="comparison")
        messages = review.review_messages([subject, comparison])
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertEqual(json.loads(messages[1]["content"])["subject"]["fact_id"], subject.id)
        self.assertEqual(json.loads(messages[1]["content"])["comparison"]["fact_id"], comparison.id)
        self.assertIsNone(json.loads(review.review_messages([subject])[1]["content"])["comparison"])
        self.assertNotIn("must review", messages[0]["content"].lower())
        self.assertIn(review.memory_task_prompt({}, "review_prompt"), messages[0]["content"])

    def test_unique_ephemeral_legacy_notes_need_no_duplicate_or_whitelist_match(self):
        for category in ("relationship", "habit"):
            with self.subTest(category=category):
                note = fact("They put the imaginary brass key into a paper sleeve in the scene.",
                            category=category)
                decision = self.decision(note, evidence_quote="imaginary brass key")
                self.assertEqual((decision["action"], decision["archive_reason"]), ("archive", "low_value"))
                self.assertEqual(decision["evidence_quote"], "imaginary brass key")
        speculation = fact("The character guessed that the guest secretly feared rejection.",
                           category="relationship")
        self.assertEqual(self.decision(speculation, archive_reason="unsupported_inference")["archive_reason"],
                         "unsupported")

    def test_unresolved_reference_can_retire_but_actionable_rule_is_protected(self):
        vague = fact("\u4ed6\u63d0\u5230\u8fc7\u90a3\u79cd\u611f\u89c9\u3002", category="relationship")
        rule = fact("\u5173\u4e8e\u90a3\u79cd\u611f\u89c9\uff0c\u672a\u7ecf\u6211\u540c\u610f\u4e0d\u8981\u78b0\u6211\u7684\u80a9\u3002",
                    category="relationship")
        self.assertEqual(self.decision(vague, archive_reason="unresolved_reference")["archive_reason"],
                         "unsupported")
        retained = self.decision(rule, archive_reason="unresolved_reference")
        self.assertEqual(retained["action"], "keep")
        self.assertIsNone(retained["archive_reason"])

    def test_mixed_update_preserves_literal_prohibition_and_its_condition(self):
        transient = "I put the brass bell back in the prop box."
        boundary = "In real life, unless I say yes, do not touch my shoulder."
        note = fact(f"{transient} {boundary}")
        allowed = self.decision(note, "update", keep_text=boundary, future_use="Before any physical contact")
        self.assertEqual(allowed["action"], "update")
        self.assertEqual(allowed["new_content"], boundary)
        for excerpt in (transient, "do not touch my shoulder."):
            with self.subTest(excerpt=excerpt):
                kept = self.decision(note, "update", keep_text=excerpt, future_use="Before any physical contact")
                self.assertEqual(kept["action"], "keep")
                self.assertNotIn("new_content", kept)
        self.assertEqual(self.decision(note, evidence_quote=transient)["action"], "keep")

    def test_quoted_conditions_and_dates_cannot_be_cut_out(self):
        opening = "I dusted the props."
        for ending in ('They said, "If I am wearing headphones, tap the table first."',
                       'They said, "Remember, if I am wearing headphones, tap the table first."',
                       "You can touch my hand with my explicit permission.",
                       "The agreement was made on 2024-04-17.",
                       "The agreement was made on April 17."):
            with self.subTest(ending=ending):
                note = fact(f"{opening} {ending}")
                self.assertEqual(self.decision(note, "update", keep_text=opening,
                                               future_use="While cleaning the props")["action"], "keep")

    def test_explicit_disabled_language_rule_survives_archive_and_shortening(self):
        transient = "\u5979\u6536\u8d77\u4e86\u9053\u5177\u3002"
        boundary = "\u7981\u7528\u653b\u51fb\u6027\u8a00\u8bed\u3002"
        note = fact(transient + boundary, category="relationship")
        self.assertEqual(self.decision(note)["action"], "keep")
        self.assertEqual(self.decision(note, "update", keep_text=transient,
                                       future_use="When continuing the scene")["action"], "keep")
        retained = self.decision(note, "update", keep_text=boundary, future_use="When choosing wording")
        self.assertEqual(retained["action"], "update")
        self.assertEqual(retained["new_content"], boundary)

    def test_protected_records_fail_closed(self):
        for fields in ({"pinned": True}, {"is_manual": True}, {"source": "manual"},
                       {"kind": "address"}, {"curation_protected": True}, {"event_date": "2024-04-17"}):
            with self.subTest(fields=fields):
                note = fact("The imaginary key went back into its sleeve.", **fields)
                self.assertEqual(self.decision(note)["action"], "keep")

    def test_relative_scene_time_and_reality_label_are_not_archive_vetoes(self):
        for text in ("角色承诺今晚会节制。", "用户在现实中透支了精力。", "The scene ended tonight."):
            with self.subTest(text=text):
                self.assertEqual(self.decision(fact(text))["action"], "archive")
        self.assertEqual(self.decision(fact("今晚讨论下一场会面。", kind="temporary",
                                            expires_at=4102444800.0))["action"], "keep")
        self.assertEqual(self.decision(fact("现实中禁止公开角色资料。"))["action"], "keep")

    def test_concrete_calendar_date_and_real_world_clause_are_protected(self):
        event = fact("On April 17, 2024, I completed the licensure exam.")
        self.assertEqual(self.decision(event)["action"], "keep")
        note = fact("In real life my sibling lives in Toronto. I prefer tea.")
        decision = self.decision(note, "update", keep_text="I prefer tea.",
                                 future_use="When choosing a drink")
        self.assertEqual(decision["action"], "keep")

    def test_editable_guidance_keeps_the_fixed_response_protocol(self):
        subject = fact("The curtain fell after the scene.")
        messages = review.review_messages([subject], review_prompt="KEEP SHORT NOTES ONLY IN THE ARCHIVE")
        system = messages[0]["content"]
        self.assertIn("KEEP SHORT NOTES ONLY IN THE ARCHIVE", system)
        self.assertIn("evidence_quote", system)
        self.assertIn("fact_id", system)
        self.assertIn("archive_reason", system)
        self.assertIn("unsupported_inference", system)
        self.assertNotIn("Keep durable preferences", system)

    def test_missing_review_guidance_does_not_build_a_model_request(self):
        with patch.object(review, "memory_task_prompt", return_value=""):
            with self.assertRaisesRegex(ValueError, "instructions are missing"):
                review.review_messages([fact("The curtain fell after the scene.")])

    def test_cold_archive_uses_existing_experience_provenance_gate(self):
        note = fact("We discussed how the recital rehearsal went.", kind="experience",
                    occurred_from="2024-04-17",
                    evidence=[{"source": "single", "id": 7, "timestamp": 1713398400,
                               "speaker_role": "user", "assertion": "self_statement"}])
        decision = self.decision(note, archive_reason="cold_memory", future_use="When revisiting the recital")
        self.assertEqual(decision["archive_reason"], "cold_memory")
        note.evidence.clear()
        self.assertEqual(self.decision(note, archive_reason="cold_memory")["action"], "keep")

    def test_merge_is_exact_durable_and_date_compatible(self):
        source = fact("They prefer unsweetened tea.", kind="stable", category="preference")
        target = fact("They prefer unsweetened tea.", id="target", kind="stable", category="preference")
        merged = self.decision(source, "merge", target, target_id=target.id, future_use="When offering tea")
        self.assertEqual((merged["action"], merged["target_id"], merged["target_content"]),
                         ("merge", target.id, target.content))
        for fields in ({"content": "They prefer unsweetened tea after lunch."},
                       {"event_date": "2024-04-18"}, {"category": "habit"}, {"pinned": True}):
            with self.subTest(fields=fields):
                other = deepcopy(target)
                for key, value in fields.items():
                    setattr(other, key, value)
                kept = self.decision(source, "merge", other, target_id=other.id,
                                     future_use="When offering tea")
                self.assertEqual(kept["action"], "keep")
                self.assertIsNone(kept["target_id"])

    def test_malformed_or_out_of_scope_responses_are_rejected(self):
        note = fact("They put the brass bell back into the prop box.")
        comparison = fact("The lantern was dimmed.", id="comparison")
        archive = proposal(note)
        cases = (
            [], [archive, archive], {"items": [archive]}, [None],
            [{**archive, "fact_id": comparison.id}],
            [{**archive, "evidence_quote": "invented words"}],
            [{**archive, "archive_reason": "low_value"}],
            [{**archive, "future_use": False}],
            [proposal(note, "update", keep_text="invented content", future_use="In a future scene")],
            [proposal(note, "merge", target_id="unknown", future_use="In a future scene")],
            [proposal(note, "review")],
        )
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                review.validate_review([note, comparison], raw)
        with self.assertRaises(ValueError):
            review.validate_review([note], [proposal(note, "merge", target_id=comparison.id)])
        for rows in ([], [note, note], [note, comparison, fact("Third fact.", id="third")]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                review.review_messages(rows)

    def test_unsupported_or_noop_change_keeps_without_a_review_task(self):
        note = fact("The prop box was locked after the scene.")
        self.assertEqual(self.decision(note, future_use="When restarting the scene")["action"], "keep")
        self.assertEqual(self.decision(note, "update", keep_text=note.content, future_use="Later")["action"],
                         "keep")
        self.assertEqual(self.decision(note, "update", keep_text="The prop box was locked", future_use="")["action"],
                         "keep")

    def test_ordered_key_and_validation_replay_are_stable_without_mutation(self):
        subject = fact("The curtain fell after the scene.")
        comparison = fact("A lantern fell after the scene.", id="comparison")
        rows = [subject, comparison]
        raw = [proposal(subject)]
        before = deepcopy(rows)
        key = review.review_key(rows, "preset-a")
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertEqual(key, review.review_key(deepcopy(rows), "preset-a"))
        self.assertNotEqual(key, review.review_key(list(reversed(rows)), "preset-a"))
        self.assertNotEqual(key, review.review_key(rows, "preset-b"))
        self.assertEqual(review.validate_review(rows, raw), review.validate_review(rows, raw))
        self.assertEqual(rows, before)
        comparison.versions.append({"content": "Earlier"})
        self.assertNotEqual(key, review.review_key(rows, "preset-a"))
