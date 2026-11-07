"""Offline comparison selection for batched fact review."""
from dataclasses import replace
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from src.memory.long_term_store import LongTermFact
from src.memory.review_neighbors import find_review_comparison
from src.memory.vector_store import VectorMemoryStore


def fact(fact_id, content, **fields):
    defaults = {"source": "auto", "is_manual": False, "kind": "stable",
                "category": "preference", "updated_at": 1.0}
    return LongTermFact(content, id=fact_id, **{**defaults, **fields})


def stored(fact, embedding, **fields):
    return {"id": fact.id, "document": fact.content, "metadata": {"fact_id": fact.id},
            "embedding": embedding, **fields}


class ReviewComparisonTests(TestCase):
    def vectors(self, *rows):
        def get(*, where, include):
            self.assertEqual(set(include), {"documents", "metadatas", "embeddings"})
            fact_ids = where["fact_id"]["$in"]
            selected = [row for row in rows if row["metadata"]["fact_id"] in fact_ids]
            return {
                "ids": [row["id"] for row in selected],
                "documents": [row["document"] for row in selected],
                "metadatas": [row["metadata"] for row in selected],
                "embeddings": [row["embedding"] for row in selected],
            }

        vectors = VectorMemoryStore(self.enterContext(TemporaryDirectory()), {}, existing_only=True)
        vectors._available = True
        vectors._collection = Mock(spec_set=["get"])
        vectors._collection.get.side_effect = get
        return vectors

    def test_exact_representative_is_stable_across_the_full_snapshot(self):
        subject = fact("z", "  ＪＡＳＭＩＮＥ   tea without sugar！")
        duplicate = fact("b", "Jasmine tea without sugar.", kind="", weight=2.0)
        representative = fact("a", "jasmine tea without sugar")
        near = fact("0", "Jasmine tea without added sugar.")
        facts = [subject, duplicate, near, representative]
        for snapshot in (facts, list(reversed(facts))):
            self.assertIs(find_review_comparison(subject, snapshot), representative)
            self.assertIs(find_review_comparison(duplicate, snapshot), representative)
            self.assertIs(find_review_comparison(representative, snapshot), near)
            self.assertIsNone(find_review_comparison(near, snapshot))

    def test_exact_representative_preserves_user_evidence(self):
        subject = fact("z", "Green tea with lemon.",
                       evidence=[{"speaker_role": "user", "assertion": "self_statement"}])
        no_evidence = replace(subject, id="a", evidence=[])
        representative = replace(subject, id="b")
        facts = [subject, no_evidence, representative]
        self.assertIs(find_review_comparison(subject, facts), representative)
        self.assertIs(find_review_comparison(no_evidence, facts), representative)
        self.assertIsNone(find_review_comparison(representative, facts))

    def test_excludes_ineligible_candidates_and_subjects(self):
        subject = fact("z", "Jasmine tea without sugar.")
        excluded = (
            {"archived": True}, {"pinned": True}, {"source": "manual"},
            {"is_manual": True}, {"curation_protected": True}, {"kind": "address"},
            {"kind": "experience"}, {"kind": "event"}, {"kind": "temporary"},
            {"category": "habit"}, {"expires_at": 0.0}, {"expires_at": 4102444800.0},
            {"event_date": "2026-11-07"}, {"occurred_from": "2026-11-07"},
            {"content": "Jasmine tea without sugar today."},
            {"content": "Jasmine tea without sugar on November 7."},
            {"content": "Jasmine tea without sugar on 2026-11-07."},
        )
        for fields in excluded:
            with self.subTest(fields=fields):
                candidate = replace(subject, id="a", **fields)
                self.assertIsNone(find_review_comparison(subject, [candidate]))
                self.assertIsNone(find_review_comparison(replace(candidate, id="z"), [replace(subject, id="a")]))

    def test_lexical_near_matches_require_substantive_body_overlap(self):
        subject = fact("z", "Jasmine tea with lemon and honey.")
        near = fact("b", "Jasmine tea with lemon.")
        equally_near = replace(near, id="a", content="Jasmine tea with honey.")
        weak = fact("0", "Black tea goes with apple pie.", tags=["jasmine", "lemon", "honey"])
        self.assertIsNone(find_review_comparison(subject, [subject, weak]))
        self.assertIs(find_review_comparison(subject, [near, weak, equally_near]), equally_near)

    def test_local_vectors_find_paraphrases_and_reject_weak_hits(self):
        subject = fact("z", "Enjoys cold brew.")
        semantic = fact("b", "Fond of chilled coffee.")
        unrelated = fact("a", "Prefers audiobooks.")
        vectors = self.vectors(stored(subject, [1, 0]), stored(semantic, [1, 0.1]),
                               stored(unrelated, [0, 1]))
        self.assertIs(find_review_comparison(subject, [unrelated, semantic], vectors), semantic)
        self.assertIsNone(find_review_comparison(subject, [unrelated], vectors))

    def test_rrf_rewards_a_candidate_with_both_lexical_and_vector_signal(self):
        subject = fact("z", "Jasmine tea with lemon and honey.")
        lexical = fact("a", "Jasmine tea with lemon and local honey.")
        semantic = fact("b", "Fond of floral beverages.")
        both = fact("c", "Jasmine tea with honey.")
        vectors = self.vectors(stored(subject, [1, 0]), stored(semantic, [1, 0.01]), stored(both, [1, 0.2]))
        self.assertIs(find_review_comparison(subject, [lexical, semantic, both], vectors), both)

    def test_missing_stale_and_invalid_vectors_fall_back_to_lexical(self):
        subject = fact("z", "Jasmine tea with lemon and honey.")
        lexical = fact("c", "Jasmine tea with lemon.")
        semantic = fact("a", "Fond of floral beverages.", tags=["flowers"])
        facts = [lexical, semantic]
        subject_row = stored(subject, [1, 0])
        semantic_row = stored(semantic, [1, 0], document="Fond of floral beverages. [关键词: flowers]")
        for rows in (
            (semantic_row,),
            ({**subject_row, "document": "Stale subject"}, semantic_row),
            (subject_row, stored(semantic, [1, 0])),  # Missing current tags.
            (subject_row, {**semantic_row, "embedding": [float("nan"), 0]}),
            (subject_row, {**semantic_row, "embedding": [0, 0]}),
            (subject_row, {**semantic_row, "embedding": [1, 0, 0]}),
        ):
            with self.subTest(rows=rows):
                self.assertIs(find_review_comparison(subject, facts, self.vectors(*rows)), lexical)

        unavailable = self.vectors()
        unavailable._collection.get.side_effect = RuntimeError("index unavailable")
        self.assertIs(find_review_comparison(subject, facts, unavailable), lexical)
