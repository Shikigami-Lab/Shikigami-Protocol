import asyncio
import re
import time
from dataclasses import replace
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from src.commands.memory_cmds import handle_recall
from src.core.user_portrait import _get_facts_text
from src.memory.curation import MemoryCuration
from src.memory.long_term_store import LongTermFact
from src.memory.memory_manager import MemoryManager, _vector_document_for_fact
from src.memory.recall import semantic_version
from src.prompt.base import BuildContext
from src.prompt.segments.long_term_facts import LongTermFactsSegment


class OfflineVectors:
    def __init__(self, *rows):
        self.rows = list(rows)
        self.queries = []
        self.limits = []
        self.before_results = None
        self.added = []

    def is_available(self):
        return True

    def search(self, query, limit):
        self.queries.append(query)
        self.limits.append(limit)
        if self.before_results:
            self.before_results()
        return self.rows[:limit]

    def get_all(self, *, strict=False):
        return self.rows.copy()

    def delete(self, vector_id):
        self.rows = [row for row in self.rows if row["id"] != vector_id]
        return True

    def delete_by_fact_ids(self, ids, *, keep_id=None):
        self.rows = [row for row in self.rows
                     if row["id"] == keep_id or (row.get("metadata") or {}).get("fact_id") not in ids]

    def upsert_fact(self, document, metadata):
        fact_id = metadata["fact_id"]
        self.added.append(fact_id)
        self.delete(fact_id)
        self.rows.append({"id": fact_id, "document": document, "metadata": metadata, "distance": 0.15})
        return fact_id

    def link_fact(self, vector_id, fact_id):
        for row in self.rows:
            if row["id"] == vector_id:
                row["metadata"] = {**(row.get("metadata") or {}), "fact_id": fact_id}
                return True
        return False


def fact(fact_id, content, **fields):
    return LongTermFact(id=fact_id, content=content, source="auto", is_manual=False,
                        updated_at=time.time() - 10 * 86400, **fields)


def vector(row, distance=0.15, *, document=None, vector_id=None):
    return {"id": vector_id or row.id, "metadata": {"fact_id": row.id}, "distance": distance,
            "document": _vector_document_for_fact(row) if document is None else document}


def write_facts(manager, *rows):
    revision, cursors, _ = manager.facts.extraction_snapshot()
    assert manager.facts.apply_extraction(revision, list(rows), cursors)


def archive(row, reason, target=None):
    return replace(row, archived=True, archive_reason=reason, merged_into=target)


def merge_alias(row, target):
    return replace(archive(row, "merged", target.id),
                   merged_source_version=semantic_version(row),
                   merged_target_version=semantic_version(target))


def app_and_session(manager):
    return (SimpleNamespace(state=SimpleNamespace(memory_managers={"fixture": manager})),
            SimpleNamespace(profile_id="fixture"))


def chat(manager, query):
    app, session = app_and_session(manager)
    ctx = BuildContext(session, {}, None, query, extras={"app": app})
    result = LongTermFactsSegment().build(ctx)
    return "\n".join(message["content"] for message in result.messages)


def command(manager, query):
    app, session = app_and_session(manager)
    return asyncio.run(handle_recall(query, session, app)).context_for_llm


def prompt_fact_lines(prompt):
    return re.findall(r"^\d+\. (.+?)（", prompt, re.MULTILINE)


class CurationRecallTests(TestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())
        with patch("src.memory.memory_manager.log_memory_startup"):
            self.manager = MemoryManager("fixture", self.root, profile_memory_cfg={
                "enabled": True, "semantic_fact_quota": 8,
            })
        self.manager.bm25 = None  # Keep lexical fallback and vector results deterministic.

    def test_semantic_version_tracks_content_fields_and_ignores_bookkeeping(self):
        original = fact("version", "Likes walks by the sea", category="preference", kind="stable")
        version = semantic_version(original)
        self.assertEqual(version, semantic_version(LongTermFact.from_dict(original.to_dict())))
        self.assertEqual(version, semantic_version(replace(
            original, updated_at=time.time(), weight=0.5, pinned=True, review_note="Check later",
        )))
        for changes in (
            {"content": "Dislikes walks by the sea"}, {"category": "habit"}, {"kind": "event"},
            {"event_date": "2026-10-01"}, {"expires_at": 123.0},
        ):
            with self.subTest(changes=changes):
                self.assertNotEqual(version, semantic_version(replace(original, **changes)))

    def test_same_query_model_input_before_archive_after_archive_and_restore(self):
        manager = self.manager
        query = "boardwalk"
        low = fact("low", "Waved once on the boardwalk")
        cold = fact("cold", "Watched the sunrise on the boardwalk together")
        manager = self.manager
        canonical = fact("main", "Prefers walks by the sea", category="preference", kind="stable")
        duplicate = fact("duplicate", "Likes walking along the boardwalk", category="preference", kind="stable")
        unsupported = fact("unsupported", "Unsupported diagnosis about the boardwalk")
        write_facts(manager, low, cold, canonical, duplicate, unsupported)
        manager.vectors = OfflineVectors(*(vector(row) for row in manager.facts.get_all()))

        before = chat(manager, query)
        self.assertCountEqual(prompt_fact_lines(before), [row.content for row in manager.facts.get_all()])

        curation = MemoryCuration("fixture", manager.facts)

        async def propose(rows):
            actions = {
                low.id: {"action": "archive", "archive_reason": "low_value"},
                cold.id: {"action": "archive", "archive_reason": "cold_memory"},
                duplicate.id: {"action": "merge", "target_id": canonical.id},
                unsupported.id: {"action": "archive", "archive_reason": "unsupported"},
            }
            return [{"fact_id": row.id, "reason": "Offline test decision",
                     **actions.get(row.id, {"action": "keep"})} for row in rows]

        preview = asyncio.run(curation.preview(propose))
        receipt = curation.apply(preview["preview_id"])
        assert (receipt["merged"], receipt["archived"]) == (1, 3)
        assert len(manager.facts.get_all()) == 5
        after = chat(manager, query)
        self.assertCountEqual(prompt_fact_lines(after), [cold.content, canonical.content])

        explicit = command(manager, query)
        assert cold.content in explicit
        assert explicit.count(canonical.content) == 1
        assert all(row.content not in explicit for row in (low, duplicate, unsupported))

        assert not chat(manager, "")
        assert {row["content"] for row in manager.get_reflection_context_facts()} == {canonical.content}
        assert {row["content"] for row in manager.get_reflection_context_facts(query="boardwalk")} == {
            canonical.content, cold.content,
        }
        portrait = _get_facts_text(self.root)
        assert canonical.content in portrait
        assert cold.content not in portrait

        curation.restore(low.id, manager.facts.get_by_id(low.id).archive_revision)
        restored = chat(manager, query)
        self.assertCountEqual(prompt_fact_lines(restored), [low.content, cold.content, canonical.content])
        self.assertIn(low.content, command(manager, query))


    def test_only_strong_cold_vectors_and_valid_aliases_reach_the_prompt(self):
        for render in (chat, command):
            with self.subTest(render=render.__name__):
                self._only_strong_cold_vectors_and_valid_aliases_reach_the_prompt(render)


    def _only_strong_cold_vectors_and_valid_aliases_reach_the_prompt(self, render):
        manager = self.manager
        canonical = fact("main", "Prefers walks by the sea")
        alias = merge_alias(fact("alias", "Likes strolling at the pier"), canonical)
        cold = archive(fact("cold", "Watched a sunrise together", pinned=True), "cold_memory")
        unknown = archive(fact("unknown", "Unknown archive content"), "unknown")
        expired = archive(fact("expired", "A previous plan", expires_at=time.time() - 1), "cold_memory")
        write_facts(manager, canonical, alias, cold, unknown, expired)
        manager.vectors = OfflineVectors(
            vector(unknown), vector(expired), vector(canonical, 0.25), vector(cold, 0.8),
        )

        rendered = render(manager, "boardwalk")
        assert canonical.content in rendered
        assert all(row.content not in rendered for row in (alias, unknown, expired, cold))

        manager.vectors.rows[-1] = vector(cold, 0.3)
        assert cold.content in render(manager, "boardwalk")
        assert not chat(manager, "")


    def test_keyword_alias_resolves_without_vectors_and_stops_at_unavailable_target(self):
        manager = self.manager
        canonical = fact("main", "Prefers walks by the sea")
        duplicate = merge_alias(fact("duplicate", "Likes walking along the boardwalk"), canonical)
        cold = archive(fact("cold", "Boardwalk sunrise together"), "cold_memory")
        write_facts(manager, canonical, duplicate, cold)
        assert canonical.content in chat(manager, "boardwalk")
        assert canonical.content in command(manager, "boardwalk")
        assert cold.content in chat(manager, "boardwalk")
        assert not chat(manager, "recommend a movie")

        write_facts(manager, archive(canonical, "cold_memory"), duplicate)
        assert not chat(manager, "boardwalk")
        assert canonical.content not in command(manager, "boardwalk")
        assert manager.facts.delete(canonical.id)
        assert not chat(manager, "boardwalk")

    def test_stale_alias_and_deleted_representative_vectors_cannot_leak(self):
        manager = self.manager
        canonical = fact("main", "Prefers walks by the sea")
        alias = merge_alias(fact("alias", "Likes strolling at the pier"), canonical)
        control = fact("control", "Enjoys coastal scenery")
        write_facts(manager, canonical, alias, control)
        manager.vectors = OfflineVectors(vector(alias, document="Previous alias wording"))

        for render in (chat, command):
            with self.subTest(render=render.__name__):
                rendered = render(manager, "boardwalk")
                self.assertNotIn(canonical.content, rendered)
                self.assertNotIn(alias.content, rendered)
                self.assertNotIn("Previous alias wording", rendered)

        manager.vectors.rows = [vector(alias), vector(canonical), vector(control)]
        self.assertTrue(manager.facts.delete(canonical.id))
        for render in (chat, command):
            with self.subTest(render=render.__name__):
                rendered = render(manager, "boardwalk")
                self.assertIn(control.content, rendered)
                self.assertNotIn(canonical.content, rendered)
                self.assertNotIn(alias.content, rendered)

        result = asyncio.run(manager.maintain_fact_index())
        self.assertEqual(result["vectors_removed"], 2)
        self.assertEqual(manager.vectors.rows, [vector(control)])

    def test_alias_requires_current_versions_on_both_ends(self):
        manager = self.manager
        canonical = fact("main", "Prefers walks by the sea")
        source = fact("alias", "Likes strolling along the boardwalk")
        alias = merge_alias(source, canonical)
        cases = [
            (replace(canonical, weight=0.5), alias, True),
            (replace(canonical, content="Prefers hikes in the mountains"), alias, False),
            (canonical, replace(alias, category="habit"), False),
            (canonical, archive(source, "merged", canonical.id), False),
        ]
        for target, stored_alias, should_resolve in cases:
            for render in (chat, command):
                with self.subTest(target=target.content, alias=stored_alias.category,
                                  versioned=bool(stored_alias.merged_source_version), render=render.__name__):
                    write_facts(manager, target, stored_alias)
                    # Even a vector matching the edited source cannot revive an invalid merge.
                    manager.vectors = OfflineVectors(vector(stored_alias))
                    rendered = render(manager, "boardwalk")
                    self.assertEqual(target.content in rendered, should_resolve)
                    self.assertNotIn(stored_alias.content, rendered)

    def test_legacy_manual_vector_is_imported_without_embedding_and_cannot_revive(self):
        manager = self.manager
        self.assertEqual(manager.facts.get_all(), [])
        document = "Standalone manual note about the coast"
        manager.vectors = OfflineVectors(
            {"id": "orphan", "metadata": {"fact_id": "deleted"}, "distance": 0.1, "document": "Deleted text"},
            {"id": "malformed", "metadata": {"fact_id": ""}, "distance": 0.15, "document": "Malformed linked text"},
            {"id": "manual", "metadata": {"created_at": time.time()}, "distance": 0.2, "document": document},
        )
        manager.import_legacy_vectors()
        manager.import_legacy_vectors()
        [manual] = manager.facts.get_all()
        self.assertTrue(manual.is_manual)
        self.assertEqual(manager.vectors.added, [])
        rendered = command(manager, "1 coast")
        self.assertIn(document, rendered)
        self.assertNotIn("Deleted text", rendered)
        self.assertNotIn("Malformed linked text", rendered)
        self.assertIn(document, chat(manager, "coast"))
        self.assertTrue(manager.facts.delete(manual.id))
        self.assertNotIn(document, command(manager, "coast"))
        asyncio.run(manager.maintain_fact_index())
        manager.import_legacy_vectors()
        self.assertEqual(manager.facts.get_all(), [])
        self.assertEqual(manager.vectors.rows, [])

    def test_single_bounded_search_gets_past_stale_archive_hits(self):
        manager = self.manager
        manager._cfg["semantic_fact_quota"] = 2
        archived = [archive(fact(f"old-{index}", f"Stale boardwalk row {index}"), "low_value")
                    for index in range(12)]
        current = fact("current", "Enjoys walks by the sea")
        write_facts(manager, *archived, current)
        for render, query in ((chat, "boardwalk"), (command, "2 boardwalk")):
            with self.subTest(render=render.__name__):
                manager.vectors = OfflineVectors(*(vector(row, 0.05) for row in archived), vector(current))
                rendered = render(manager, query)
                self.assertIn(current.content, rendered)
                self.assertNotIn("Stale boardwalk row", rendered)
                self.assertEqual(manager.vectors.queries, ["boardwalk"])
                self.assertLessEqual(manager.vectors.limits[0], 60)


    def test_vector_search_rereads_archive_state_and_legacy_text_before_rendering(self):
        for render in (chat, command):
            with self.subTest(render=render.__name__):
                self._vector_search_rereads_archive_state_and_legacy_text_before_rendering(render)


    def _vector_search_rereads_archive_state_and_legacy_text_before_rendering(self, render):
        manager = self.manager
        old = fact("legacy", "Previous memory text")
        now_archived = fact("transition", "Needle formerly active")
        valid = fact("valid", "Current semantically related memory", tags=["tag"])
        write_facts(manager, old, now_archived, valid)
        manager.vectors = OfflineVectors(
            vector(old), vector(now_archived), vector(valid),
            {"id": "unlinked", "metadata": {}, "document": "Standalone manual vector text", "distance": 0.1},
        )

        def update_while_searching():
            write_facts(manager, replace(old, content="New unrelated text"),
                        archive(now_archived, "unsupported"), valid)

        manager.vectors.before_results = update_while_searching
        rendered = render(manager, "needle")
        assert manager.vectors.queries == ["needle"]
        assert valid.content in rendered
        assert _vector_document_for_fact(valid) not in rendered
        assert old.content not in rendered
        assert "New unrelated text" not in rendered
        assert now_archived.content not in rendered
        self.assertNotIn("Standalone manual vector text", rendered)


    def test_index_cleanup_and_sync_keep_one_row_per_recallable_fact(self):
        manager = self.manager
        canonical = fact("main", "Prefers walks by the sea")
        alias = merge_alias(fact("alias", "Likes boardwalks"), canonical)
        cold = archive(fact("cold", "Boardwalk sunrise together"), "cold_memory")
        missing_cold = archive(fact("missing-cold", "Went to the sea together"), "cold_memory")
        missing_alias = merge_alias(fact("missing-alias", "Enjoys coastal walks"), canonical)
        low = archive(fact("low", "Waved once on the boardwalk"), "low_value")
        broken = archive(fact("broken", "Broken boardwalk alias"), "merged", "deleted")
        write_facts(manager, canonical, alias, cold, missing_cold, missing_alias, low, broken)
        manager.vectors = OfflineVectors(
            vector(canonical), vector(canonical, vector_id="duplicate"), vector(alias), vector(cold), vector(low), vector(broken),
            vector(cold, document="Previous cold text", vector_id="stale"),
        )
        assert asyncio.run(manager.maintain_fact_index()) == {"active": 1, "expired": 0, "vectors_removed": 5}
        assert {row["id"] for row in manager.vectors.rows} == {"main", "cold"}
        assert asyncio.run(manager.sync_facts_to_vectors()) == 1
        assert manager.vectors.added == ["missing-cold"]

        assert asyncio.run(manager.sync_facts_to_vectors()) == 0
        assert {row["id"] for row in manager.vectors.rows} == {"main", "cold", "missing-cold"}

    def test_keyword_recall_without_vector_survives_sync_then_stops_after_forgetting(self):
        manager = self.manager
        row = fact("tea", "用户喝乌龙茶不加糖")
        write_facts(manager, row)
        self.assertIn(row.content, chat(manager, "乌龙茶"))
        self.assertIn(row.content, command(manager, "乌龙茶"))
        manager.vectors = OfflineVectors()
        self.assertEqual(asyncio.run(manager.sync_facts_to_vectors()), 1)
        self.assertEqual(asyncio.run(manager.sync_facts_to_vectors()), 0)
        write_facts(manager, archive(row, "low_value"))
        self.assertNotIn(row.content, chat(manager, "乌龙茶"))
        self.assertNotIn(row.content, command(manager, "乌龙茶"))
        asyncio.run(manager.maintain_fact_index())
        self.assertEqual(manager.vectors.rows, [])
        self.assertEqual(len(manager.facts.get_all()), 1)

    def test_retrying_legacy_link_after_fact_deletion_does_not_restore_the_note(self):
        manager = self.manager
        manager.vectors = OfflineVectors({"id": "manual", "metadata": {}, "document": "Old tea note"})
        with patch.object(manager.vectors, "link_fact", return_value=False):
            manager.import_legacy_vectors()
        [row] = manager.facts.get_all()
        self.assertTrue(manager.facts.delete(row.id))
        with patch("src.memory.memory_manager.log_memory_startup"):
            reopened = MemoryManager("fixture", self.root)
        reopened.vectors = manager.vectors
        reopened.import_legacy_vectors()
        asyncio.run(reopened.maintain_fact_index())
        self.assertEqual(reopened.facts.get_all(), [])
        self.assertEqual(reopened.vectors.rows, [])

    def test_legacy_note_matching_forgotten_text_is_preserved_without_reviving_it(self):
        manager = self.manager
        row = fact("old", "Tea without sugar")
        write_facts(manager, archive(row, "low_value"))
        manager.vectors = OfflineVectors({"id": "manual", "metadata": {}, "document": row.content})
        manager.import_legacy_vectors()
        [original, imported] = manager.facts.get_all()
        self.assertTrue(original.archived)
        self.assertTrue(imported.archived)
        self.assertTrue(imported.is_manual)
        self.assertNotEqual(original.id, imported.id)
        self.assertNotIn(row.content, command(manager, "Tea"))
        asyncio.run(manager.maintain_fact_index())
        self.assertEqual(manager.vectors.rows, [])

    def test_failed_index_scan_leaves_curation_cleanup_pending(self):
        manager = self.manager
        manager.vectors = OfflineVectors()
        revision, cursors, facts, state = manager.facts.curation_snapshot()
        state["index_cleanup_pending"] = "pending-fixture"
        self.assertTrue(manager.facts.apply_curation(revision, facts, cursors, state))
        with patch.object(manager.vectors, "get_all", side_effect=OSError("index is busy")):
            asyncio.run(manager.prune_curation_index())
        self.assertEqual(manager.facts.curation_index_token(), "pending-fixture")

    def test_clear_facts_while_index_is_unavailable_prevents_later_legacy_import(self):
        manager = self.manager
        write_facts(manager, fact("existing", "Current tea note"))
        self.assertEqual(manager.facts.clear_all(), 1)
        manager._offline_index = OfflineVectors({"id": "manual", "metadata": {}, "document": "Old tea note"})
        asyncio.run(manager.prune_curation_index())
        self.assertEqual(manager._offline_index.rows, [])
        self.assertIsNone(manager.facts.curation_index_token())
        manager.import_legacy_vectors()
        self.assertEqual(manager.facts.get_all(), [])

    def test_offline_index_migrates_and_prunes_while_chat_uses_keywords(self):
        manager = self.manager
        manager._offline_index = OfflineVectors({"id": "manual", "metadata": {}, "document": "Old tea note"})
        manager.import_legacy_vectors()
        [row] = manager.facts.get_all()
        self.assertIsNone(manager.vectors)
        self.assertIn(row.content, chat(manager, "tea"))
        self.assertIn(row.content, command(manager, "tea"))
        self.assertEqual(manager._offline_index.queries, [])
        write_facts(manager, archive(row, "low_value"))
        self.assertNotIn(row.content, command(manager, "tea"))
        asyncio.run(manager.maintain_fact_index())
        self.assertEqual(manager._offline_index.rows, [])

    def test_legacy_note_matching_cold_memory_keeps_its_own_archived_record(self):
        manager = self.manager
        cold = archive(fact("cold", "Boardwalk sunrise together", kind="experience"), "cold_memory")
        write_facts(manager, cold)
        manager.vectors = OfflineVectors(
            vector(cold), {"id": "manual", "metadata": {}, "document": cold.content},
        )
        manager.import_legacy_vectors()
        [original, imported] = manager.facts.get_all()
        self.assertEqual(original.archive_reason, "cold_memory")
        self.assertTrue(imported.is_manual and imported.archived)
        asyncio.run(manager.maintain_fact_index())
        self.assertEqual([row["id"] for row in manager.vectors.rows], [cold.id])
        self.assertIn(cold.content, command(manager, "boardwalk"))
