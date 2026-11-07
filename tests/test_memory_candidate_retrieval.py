from copy import deepcopy
from datetime import date, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

from src.memory.conversation_store import ConversationStore
from src.memory.fact_operations import apply_operations, choose_candidates
from src.memory.long_term_store import LongTermFact
from src.memory.memory_manager import MemoryManager, _vector_document_for_fact


def fact(id, content, **fields):
    return LongTermFact(id=id, content=content, source="auto", is_manual=False, updated_at=1, **fields)


def user(text, **fields):
    return {"source": "single", "id": 1, "timestamp": 2.0, "role": "user",
            "reference": "single:1", "content": text, **fields}


def vector(fact, distance=0.1, *, document=None):
    return {"metadata": {"fact_id": fact.id}, "distance": distance,
            "document": _vector_document_for_fact(fact) if document is None else document}


class CandidateRetrievalTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())
        self.enterContext(patch("src.memory.memory_manager.log_memory_startup"))
        self.manager = MemoryManager("fixture", self.root, profile_memory_cfg={
            "enabled": True, "extraction_frequency": 1, "vector_enabled": False,
        })

    def vectors(self, rows=(), *, error=None):
        search = Mock(return_value=rows, side_effect=error)
        self.manager.vectors = SimpleNamespace(is_available=lambda: True, search=search)
        return search

    async def test_paraphrase_gets_one_search_of_user_speech_only(self):
        paraphrase = fact("semantic", "Prefers quiet rooms.")
        lexical = fact("lexical", "Libraries have enclosed cubicles.")
        rows = [user("Secluded libraries suit me."),
                {"role": "assistant", "content": "Do not search the assistant's text."}]
        self.assertEqual(choose_candidates([paraphrase, lexical], rows), [lexical])
        search = self.vectors([vector(paraphrase)])
        candidates = await self.manager.choose_extraction_candidates([paraphrase, lexical], rows)
        self.assertEqual({row.id for row in candidates}, {"semantic", "lexical"})
        search.assert_called_once()
        self.assertEqual(search.call_args.args, ("Secluded libraries suit me.",))

    async def test_disabled_failed_and_invalid_vector_results_match_lexical_order(self):
        facts = [fact("z", "Tea and cookies."), fact("a", "Tea.")]
        rows = [user("Tea and cookies")]
        expected = choose_candidates(facts, rows)
        self.assertEqual(await self.manager.choose_extraction_candidates(facts, rows), expected)
        self.manager.vectors = SimpleNamespace(is_available=lambda: False, search=Mock())
        self.assertEqual(await self.manager.choose_extraction_candidates(facts, rows), expected)
        self.manager.vectors.search.assert_not_called()
        for invalid in (None, {"items": []}, [None, {"metadata": []}, vector(facts[0], float("nan"))]):
            with self.subTest(invalid=invalid):
                search = self.vectors(invalid)
                self.assertEqual(await self.manager.choose_extraction_candidates(facts, rows), expected)
                search.assert_called_once()
        search = self.vectors(error=OSError("index offline"))
        self.assertEqual(await self.manager.choose_extraction_candidates(facts, rows), expected)
        search.assert_called_once()

    async def test_invalid_distance_setting_uses_lexical_without_searching(self):
        facts = [fact("lexical", "Tea.")]
        rows = [user("Tea.")]
        for threshold in (None, "invalid", float("nan")):
            with self.subTest(threshold=threshold):
                self.manager._cfg["semantic_distance"] = threshold
                search = self.vectors([vector(facts[0])])
                self.assertEqual(await self.manager.choose_extraction_candidates(facts, rows), facts)
                search.assert_not_called()

    async def test_stale_protected_expired_and_foreign_vectors_are_never_candidates(self):
        stale = fact("stale", "Current wording.", tags=["new"])
        protected = fact("protected", "Private wording.", pinned=True)
        expired = fact("expired", "Past appointment.", kind="event", expires_at=1)
        expired_cold = fact("expired-cold", "Old episode.", kind="experience", archived=True,
                            archive_reason="cold_memory", expires_at=1)
        cold = fact("cold", "We talked at the station.", kind="experience",
                    archived=True, archive_reason="cold_memory")
        lexical = fact("lexical", "Likes train rides.")
        search = self.vectors([vector(stale, document=stale.content), vector(protected), vector(expired),
                               vector(expired_cold), vector(fact("foreign", "Foreign record.")),
                               vector(cold, 0.12), vector(lexical, 0.7)])
        candidates = await self.manager.choose_extraction_candidates(
            [stale, protected, expired, expired_cold, cold, lexical], [user("Train rides.")])
        self.assertEqual({row.id for row in candidates}, {"cold", "lexical"})
        search.assert_called_once()

    async def test_rrf_rewards_overlap_breaks_ties_by_id_and_caps_both_inputs(self):
        lexical = [fact(f"l{i:02}", "Tea.") for i in range(17)]
        semantic = [fact(f"v{i:02}", "Infusion.") for i in range(17)]
        search = self.vectors([vector(item) for item in reversed(semantic)] + [vector(lexical[15], 0.05)])
        candidates = await self.manager.choose_extraction_candidates(
            [*reversed(lexical), *semantic], [user("Tea.")])
        self.assertEqual(len(candidates), 16)
        self.assertEqual([f.id for f in candidates[:3]], ["l15", "l00", "l01"])
        self.assertEqual([f.id for f in candidates[3:7]], ["v00", "l02", "v01", "l03"])
        search.assert_called_once()

    async def test_ranks_beyond_either_top_sixteen_do_not_get_overlap_credit(self):
        lexical = [fact(f"l{i:02}", "Tea.") for i in range(17)]
        semantic = [fact(f"v{i:02}", "Infusion.") for i in range(15)]
        self.vectors([vector(lexical[16], 0.05), *(vector(item) for item in semantic),
                      vector(lexical[15], 0.2)])
        candidates = await self.manager.choose_extraction_candidates([*lexical, *semantic], [user("Tea.")])
        self.assertEqual([f.id for f in candidates[:2]], ["l00", "l16"])
        self.assertNotIn(lexical[15], candidates)

    async def test_address_and_validated_direct_source_are_inside_the_cap(self):
        address = fact("address", "Preferred address.", kind="address")
        ref = {"source": "single", "id": 1, "timestamp": 2.0,
               "speaker_role": "user", "assertion": "self_statement", "content_hash": "same"}
        direct = fact("direct", "No lexical overlap here.", evidence=[ref])
        bad = [fact("bad-hash", "Different message.", evidence=[{**ref, "content_hash": "different"}]),
               fact("bad-time", "Different time.", evidence=[{**ref, "timestamp": 3.0}]),
               fact("bad-role", "Assistant speech.", evidence=[{**ref, "speaker_role": "assistant"}]),
               fact("bad-span", "Different slice.", evidence=[{**ref, "start_char": 1, "end_char": 3}]),
               fact("bad-protected", "Pinned.", pinned=True, evidence=[ref])]
        filler = [fact(f"l{i:02}", "Tea.") for i in range(16)]
        rows = [user("Tea.", content_hash="same")]
        pool = [*bad, *filler, direct, address]
        self.vectors([])
        lexical_only = await self.manager.choose_extraction_candidates(pool, rows)
        self.assertEqual([f.id for f in lexical_only], ["address", *(f.id for f in filler[:15])])
        self.vectors([vector(filler[0])])
        candidates = await self.manager.choose_extraction_candidates(pool, rows)
        self.assertEqual([f.id for f in candidates], ["address", "direct", *(f.id for f in filler[:14])])

    def test_direct_source_ignores_malformed_current_references(self):
        direct = fact("direct", "No lexical overlap here.", evidence=[
            {"source": "single", "id": 1, "timestamp": 2.0, "role": "user"},
        ])
        bad_hash = user("Tea.", content_hash=[])
        bad_reference = {**user("Tea."), "reference": "group:elsewhere:1"}
        for row in (bad_hash, bad_reference):
            self.assertEqual(choose_candidates([direct], [row]), [])

    async def test_event_candidates_are_scoped_to_explicit_dates(self):
        old, new = [(date.today() + timedelta(days=n)).isoformat() for n in (5, 7)]
        same = fact("same", "Interview.", kind="event", event_date=new)
        other = fact("other", "Interview.", kind="event", event_date=old)
        self.vectors([vector(other), vector(same)])
        self.assertEqual(await self.manager.choose_extraction_candidates(
            [other, same], [user(f"Interview on {new}.")]), [same])

    async def test_auto_extract_updates_a_semantic_only_candidate_through_the_normal_flow(self):
        prior = fact("existing", "Prefers quiet rooms.", kind="stable", category="preference")
        revision, _, _ = self.manager.facts.extraction_snapshot()
        self.assertTrue(self.manager.facts.apply_extraction(revision, [prior], {"single": 0}))
        search = self.vectors([vector(prior)])
        app = SimpleNamespace(state=SimpleNamespace())
        chat = ConversationStore(self.root + "/chat", max_turns=0)
        self.addCleanup(chat.close)
        message = chat.append("user", "Correction: secluded libraries suit me better.")

        async def extract(rows, profile, app, *, candidates):
            self.assertEqual([row.id for row in candidates], [prior.id])
            return [{"op": "update", "fact_id": prior.id, "expected_version": prior.record_version,
                     "content": "Prefers secluded libraries.", "evidence": [f"single:{message}"]}]

        with patch.object(self.manager, "_extract_facts_llm", side_effect=extract) as model, \
             patch.object(self.manager, "reembed_fact", new_callable=AsyncMock):
            await self.manager.auto_extract({"single": chat}, {}, app)
        search.assert_called_once()
        model.assert_awaited_once()
        current, = self.manager.facts.get_all()
        self.assertEqual((current.id, current.content), ("existing", "Prefers secluded libraries."))
        self.assertEqual(current.versions[0]["content"], prior.content)
        self.assertEqual(self.manager.facts.source_cursors(), {"single": message})


class CandidateWriteValidationTests(TestCase):
    def test_legacy_omitted_version_still_checks_the_supplied_candidate_snapshot(self):
        candidate = fact("existing", "Prefers tea.", kind="stable")
        current = deepcopy(candidate)
        current.versions.append(candidate.snapshot())
        current.content = "Prefers coffee."
        rows = {"single:1": user("I prefer tea.")}
        operation = {"op": "confirm", "fact_id": candidate.id, "evidence": ["single:1"]}
        with self.assertRaisesRegex(ValueError, "stale memory version"):
            apply_operations([current], [operation], rows, [candidate], 0.5)
        apply_operations([current], [operation], rows, [deepcopy(current)], 0.5)
        self.assertEqual(len(current.evidence), 1)

    def test_confirmation_cannot_attach_a_different_date_to_an_event(self):
        old, new = [(date.today() + timedelta(days=n)).isoformat() for n in (5, 7)]
        event = fact("event", "Scheduled interview.", kind="event", event_date=old)
        operation = {"op": "confirm", "fact_id": event.id, "event_date": old, "evidence": ["single:1"]}
        apply_operations([event], [operation], {"single:1": user(f"The interview is on {new}.")}, [event], 0.5)
        self.assertEqual(event.evidence, [])

    def test_legacy_dated_target_cannot_be_rewritten_as_an_undated_fact(self):
        old = (date.today() + timedelta(days=5)).isoformat()
        event = fact("event", "Scheduled interview.", event_date=old)
        operation = {"op": "update", "kind": "stable", "fact_id": event.id, "content": "Frequently interviews.",
                     "evidence": ["single:1"]}
        apply_operations([event], [operation], {"single:1": user("I frequently interview.")}, [event], 0.5)
        self.assertEqual((event.content, event.event_date), ("Scheduled interview.", old))
