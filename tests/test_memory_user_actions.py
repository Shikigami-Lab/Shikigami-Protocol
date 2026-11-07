"""Offline coverage for revision-checked memory actions."""
from copy import deepcopy
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from src.memory import long_term_store as memory
from src.memory.curation import MemoryCuration


def fact(**fields):
    return memory.LongTermFact(**{
        "id": "fixture", "content": "The current memory.", "source": "auto", "is_manual": False,
        "review_note": "Check whether this should remain active.", **fields,
    })


def user_evidence():
    return {"source": "single", "id": 17, "timestamp": 1713312000,
            "speaker_role": "user", "assertion": "self_statement"}


class FactActionTests(TestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())
        self.store = memory.LongTermStore(self.root)

    def save(self, record, state=None):
        revision, _, _, previous = self.store.curation_snapshot()
        self.assertTrue(self.store.apply_curation(revision, [record], {"single": 5}, state or previous))
        return record

    def current(self):
        return memory.LongTermStore(self.root).get_by_id("fixture")

    def test_revision_is_canonical_and_changes_with_any_action_relevant_state(self):
        current = self.save(fact(weight=1.2345, updated_at=42,
                                 evidence=[user_evidence()], versions=[{"content": "Earlier"}]))
        token = memory.fact_action_revision(current)
        self.assertEqual(token, memory.fact_action_revision(self.current()))
        reordered = deepcopy(current)
        reordered.evidence = [dict(reversed(list(current.evidence[0].items())))]
        self.assertEqual(token, memory.fact_action_revision(reordered))
        for key, value in (
            ("review_note", "New concern"), ("archived", True), ("pinned", True),
            ("evidence", []), ("versions", []), ("weight", 1.2346),
        ):
            with self.subTest(key=key):
                modified = deepcopy(current)
                setattr(modified, key, value)
                self.assertNotEqual(token, memory.fact_action_revision(modified))

    def test_keep_rejects_stale_or_consumed_tokens_and_protects_fact(self):
        initial = self.save(fact())
        token = memory.fact_action_revision(initial)
        other = memory.LongTermStore(self.root)
        self.assertTrue(other.update("fixture", pinned=True))
        self.assertFalse(self.store.resolve_review("fixture", token, "keep"))
        latest = memory.fact_action_revision(self.current())
        self.assertTrue(self.store.resolve_review("fixture", latest, "keep"))
        kept = self.current()
        self.assertEqual(kept.review_note, "")
        self.assertTrue(kept.pinned and kept.curation_protected)
        self.assertFalse(kept.archived)
        self.assertFalse(self.store.resolve_review("fixture", latest, "keep"))
        self.assertFalse(self.store.resolve_review("fixture", memory.fact_action_revision(kept), "keep"))

    def test_review_archive_is_reversible_and_persists_index_cleanup(self):
        original = self.save(fact())
        token = memory.fact_action_revision(original)
        self.assertTrue(self.store.resolve_review("fixture", token, "archive", "low_value"))
        archived = self.current()
        self.assertEqual(archived.archive_reason, "low_value")
        self.assertEqual(archived.archive_note, original.review_note)
        self.assertEqual(archived.review_note, "")
        self.assertEqual(archived.content, original.content)
        self.assertTrue(archived.archived and archived.curation_protected)
        self.assertTrue(archived.archive_revision)
        self.assertEqual(self.store.curation_index_token(), archived.archive_revision)
        self.assertEqual(self.store.source_cursors(), {"single": 5})
        self.assertFalse(self.store.resolve_review("fixture", token, "archive", "low_value"))
        MemoryCuration("fixture", self.store).restore("fixture", archived.archive_revision)
        self.assertFalse(self.current().archived)
        self.assertEqual(self.current().content, original.content)

    def test_invalid_or_protected_review_actions_leave_the_record_unchanged(self):
        for fields in ({"pinned": True}, {"is_manual": True}, {"source": "manual"},
                       {"archived": True}, {"expires_at": 1}, {"review_note": ""}):
            with self.subTest(fields=fields):
                original = self.save(fact(**fields))
                token = memory.fact_action_revision(original)
                self.assertFalse(self.store.resolve_review("fixture", token, "archive", "low_value"))
                self.assertEqual(self.current(), original)
        original = self.save(fact())
        token = memory.fact_action_revision(original)
        for decision, reason in (("archive", None), ("archive", "merged"), ("archive", []),
                                 ("keep", "unsupported"), ("unknown", None)):
            self.assertFalse(self.store.resolve_review("fixture", token, decision, reason))
        self.assertEqual(self.current(), original)

    def test_cold_archive_requires_experience_user_evidence_and_valid_dates(self):
        reference = user_evidence()
        for fields in (
            {"kind": "stable"}, {"evidence": []},
            {"evidence": [{**reference, "speaker_role": "assistant", "assertion": "character_context"}]},
            {"evidence": [{**reference, "id": True}]},
            {"occurred_from": "2024-04-20", "occurred_to": "2024-04-19"},
            {"occurred_from": "2099-01-01"}, {"occurred_from": "2024-02-30"},
            {"event_date": "2024-04-17"},
        ):
            with self.subTest(fields=fields):
                original = self.save(fact(**{"kind": "experience", "evidence": [reference], **fields}))
                token = memory.fact_action_revision(original)
                self.assertFalse(self.store.resolve_review("fixture", token, "archive", "cold_memory"))
                self.assertEqual(self.current(), original)
        for date in (None, "2024-04-17"):
            with self.subTest(date=date):
                current = self.save(fact(kind="experience", evidence=[reference], occurred_from=date))
                self.assertTrue(self.store.resolve_review("fixture", memory.fact_action_revision(current),
                                                          "archive", "cold_memory"))

    def test_restore_legacy_version_preserves_overrides_and_can_restore_displaced_version(self):
        historical = {"content": "Earlier memory.", "kind": "experience", "tags": ["earlier"],
                      "evidence": [{"source": "single", "id": 7}], "category": "relationship",
                      "pinned": False, "source": "reflection", "weight": 0.2, "archived": False}
        previous = self.save(fact(kind="experience", pinned=True, is_manual=True, weight=1.75,
                                  archived=True, archive_reason="low_value", archive_revision="old-token",
                                  archive_note="Earlier archive decision", tags=["newer"],
                                  occurred_from="2024-04-17", retention_reason="Newer rationale",
                                  emotional_note="Relieved", evidence=[user_evidence()], versions=[historical]))
        token = memory.fact_action_revision(previous)
        self.assertTrue(self.store.restore_version("fixture", 0, token))
        restored = self.current()
        self.assertEqual((restored.id, restored.kind, restored.source), ("fixture", "experience", "auto"))
        self.assertEqual((restored.content, restored.tags, restored.evidence, restored.category),
                         (historical["content"], historical["tags"], historical["evidence"], historical["category"]))
        self.assertEqual((restored.occurred_from, restored.occurred_to, restored.retention_reason), (None, None, ""))
        self.assertTrue(restored.pinned and restored.is_manual and restored.archived and restored.curation_protected)
        self.assertEqual((restored.weight, restored.archive_reason, restored.archive_note),
                         (previous.weight, previous.archive_reason, previous.archive_note))
        self.assertNotEqual(restored.archive_revision, previous.archive_revision)
        self.assertEqual(restored.versions, [historical, previous.snapshot()])
        self.assertFalse(self.store.restore_version("fixture", 0, token))
        self.assertTrue(self.store.restore_version("fixture", 1, memory.fact_action_revision(restored)))
        undone = self.current()
        self.assertEqual((undone.content, undone.evidence, undone.retention_reason,
                          undone.occurred_from, undone.emotional_note),
                         (previous.content, previous.evidence, previous.retention_reason,
                          previous.occurred_from, previous.emotional_note))
        self.assertEqual(undone.versions[-1], restored.snapshot())

    def test_update_after_restore_requires_current_fact_revision(self):
        previous = self.save(fact(kind="stable", versions=[{"kind": "stable", "content": "Earlier memory."}]))
        stale = memory.fact_action_revision(previous)
        self.assertTrue(self.store.restore_version(previous.id, 0, stale))
        restored = self.current()
        self.assertFalse(self.store.update(previous.id, expected_revision=stale, content="Stale edit."))
        self.assertEqual(self.current(), restored)
        self.assertTrue(self.store.update(previous.id, expected_revision=memory.fact_action_revision(restored),
                                          content="Current edit."))
        self.assertEqual(self.current().content, "Current edit.")

    def test_update_rejects_competing_writes_after_its_snapshot(self):
        for external in (False, True):
            with self.subTest(external=external):
                original = self.save(fact())
                token = memory.fact_action_revision(original)
                writer = memory.LongTermStore(self.root) if external else self.store
                commit = self.store._commit
                writer_commit = writer._commit

                def competing_write(*args, **kwargs):
                    revision, cursors, facts = writer.extraction_snapshot()
                    facts[0].pinned = True
                    self.assertTrue(writer_commit(facts, cursors, expected_revision=revision))
                    return commit(*args, **kwargs)

                with patch.object(self.store, "_commit", side_effect=competing_write):
                    self.assertFalse(self.store.update(original.id, expected_revision=token,
                                                       content="Overwritten by a race."))
                current = self.current()
                self.assertTrue(current.pinned)
                self.assertEqual(current.content, original.content)

    def test_restore_accepts_untyped_history_only_for_current_stable_fact(self):
        for old_kind in ({}, {"kind": ""}):
            for kind in ("stable", "experience", "address", "event", "temporary"):
                with self.subTest(old_kind=old_kind, kind=kind):
                    version = {"content": "Earlier memory.", **old_kind}
                    if kind == "event":
                        version["event_date"] = "2024-04-17"
                    if kind in ("event", "temporary"):
                        version["expires_at"] = 1713312000
                    previous = self.save(fact(kind=kind, versions=[version]))
                    token = memory.fact_action_revision(previous)
                    self.assertEqual(self.store.restore_version(previous.id, 0, token), kind == "stable")
                    current = self.current()
                    if kind == "stable":
                        self.assertEqual((current.id, current.kind, current.content),
                                         (previous.id, kind, version["content"]))
                        self.assertEqual(current.versions[-1], previous.snapshot())
                    else:
                        self.assertEqual(current, previous)

    def test_restore_rejects_malformed_versions_and_wrong_kind(self):
        current = self.save(fact(kind="experience", versions=[
            {"kind": "stable", "content": "Different kind."},
            {"kind": "experience"},
            {"kind": "experience", "content": "Earlier", "evidence": [{"id": 4}]},
            {"kind": "experience", "content": "Earlier", "occurred_from": "impossible"},
            "invalid snapshot",
        ]))
        token = memory.fact_action_revision(current)
        for index in (-1, 0, 1, 2, 3, 4, 5, True, "0"):
            with self.subTest(index=index):
                self.assertFalse(self.store.restore_version("fixture", index, token))
        self.assertEqual(self.current(), current)

    def test_review_racing_with_disk_change_cannot_overwrite_newer_pin(self):
        original = self.save(fact())
        token = memory.fact_action_revision(original)
        commit = self.store.apply_curation

        def competing_write(*args):
            self.assertTrue(memory.LongTermStore(self.root).update("fixture", pinned=True))
            return commit(*args)

        with patch.object(self.store, "apply_curation", side_effect=competing_write):
            self.assertFalse(self.store.resolve_review("fixture", token, "archive", "low_value"))
        current = self.current()
        self.assertTrue(current.pinned)
        self.assertFalse(current.archived)
        self.assertEqual(current.review_note, original.review_note)
        self.assertIsNone(self.store.curation_index_token())

    def test_failed_archive_write_preserves_review_and_index_state(self):
        original = self.save(fact(), {"index_cleanup_pending": "previous-job"})
        token = memory.fact_action_revision(original)
        with patch.object(memory.os, "replace", side_effect=OSError("fixture")):
            self.assertFalse(self.store.resolve_review("fixture", token, "archive", "unsupported"))
        self.assertEqual(self.current(), original)
        self.assertEqual(self.store.curation_index_token(), "previous-job")

    def test_daily_status_forwards_optional_counters_only(self):
        status = {"day": "2026-11-07", "status": "completed"}
        self.save(fact(), {"daily": {**status, "id": "internal"}})
        self.assertEqual(self.store.curation_status()["daily"], status)
        metrics = {"requests": 1, "dialogue_messages": 10, "dialogue_slices": 4, "added": 2,
                   "updated": 1, "confirmed": 3, "merged": 1, "archived": 2, "reviewed": 0}
        self.save(fact(), {"daily": {**status, "id": "internal", **metrics, "internal": "detail"}})
        self.assertEqual(self.store.curation_status()["daily"], {**status, **metrics})
