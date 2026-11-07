"""Production extraction and recall paths, with deterministic model output."""
import json
import re
from datetime import datetime
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from src.memory.conversation_store import ConversationStore
from src.memory.curation import MemoryCuration
from src.memory.memory_manager import MemoryManager, _vector_document_for_fact
from src.prompt.base import BuildContext
from src.prompt.segments.long_term_facts import LongTermFactsSegment
from src.prompt.segments.mid_term_memory import MidTermMemorySegment


class ContinuousMemoryTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())
        for name in ("log_memory_startup", "log_summary_generate"):
            self.enterContext(patch(f"src.memory.memory_manager.{name}"))
        for name in ("log_secondary_llm_call", "log_secondary_llm_response"):
            self.enterContext(patch(f"src.utils.debug_logger.{name}"))
        self.manager = self.new_manager(30)
        self.store = ConversationStore(self.root + "/chat", max_turns=0)
        self.addCleanup(self.store.close)
        self.app = SimpleNamespace(state=SimpleNamespace(
            config=SimpleNamespace(get_active_llm_preset=lambda: {"model": "synthetic"}),
            memory_managers={"fixture": self.manager},
        ))
        self.calls = []

    def new_manager(self, frequency):
        return MemoryManager("fixture", self.root, profile_memory_cfg={
            "enabled": True, "extraction_frequency": frequency, "day_summary_enabled": True,
        })

    async def turn(self, text, assistant="I heard you."):
        id = self.store.append("user", text)
        self.store.append("assistant", assistant)
        await self.manager.auto_extract({"single": self.store}, {}, self.app, first_message=("single", id))
        return id

    def chat_input(self, query):
        ctx = BuildContext(SimpleNamespace(profile_id="fixture"), {}, self.store, query,
                           extras={"app": self.app})
        segments = [LongTermFactsSegment().build(ctx), MidTermMemorySegment().build(ctx)]
        return "\n".join(msg["content"] for result in segments for msg in result.messages)

    async def test_six_hundred_turn_replay_keeps_one_preference_and_one_experience(self):
        story = [
            "We missed the ferry, walked to the pier, and watched sunrise together.",
            "About the same pier sunrise: we shared hot tea while waiting for the ferry.",
            "About that pier sunrise: later we agreed the unplanned pause was worth remembering.",
        ]
        triggers = {0: "I drink unsweetened tea.", 11: "My tea preference changed: I now add honey.",
                    12: "Pier sunrise. We missed the ferry and watched the sun come up together.",
                    13: "Same pier sunrise. We shared hot tea while waiting.",
                    14: "Same pier sunrise. We agreed the unplanned pause was worth remembering.",
                    15: "Museum plan for 2035-05-01.", 16: "A separate museum plan for 2035-05-03."}
        for i, adjective in enumerate(("plain", "without sugar", "unsweetened", "with no syrup",
                                      "unflavored", "nothing sweet", "without sweetener", "plain again",
                                      "only leaves and water", "without additives"), 1):
            triggers[i] = f"For my tea I like it {adjective}."

        async def stream(messages):
            self.calls.append(messages)
            batch = len(self.calls) - 1
            if batch not in triggers:
                yield "[]"
                return
            data = messages[-1]["content"]
            line = next(line for line in data.splitlines() if triggers[batch] in line)
            ref = re.match(r"\[([^]]+)\]", line).group(1)
            if batch <= 11:
                previous = next((f for f in self.manager.facts.get_all() if f.category == "preference"), None)
                action = "add" if previous is None else "update" if batch == 11 else "confirm"
                item = dict(op=action, kind="stable", category="preference", evidence=[ref],
                            content="They now prefer tea with honey." if batch == 11 else "They prefer unsweetened tea.")
            elif batch <= 14:
                previous = next((f for f in self.manager.facts.get_all() if f.kind == "experience"), None)
                item = dict(op="update" if previous else "add", kind="experience", category="milestone",
                            evidence=[ref], retention_reason="A warm shared memory worth recalling later",
                            content=" ".join(story[:batch-11]), keywords=["pier sunrise", "ferry", "harbor"])
            else:
                previous = None
                date = "2035-05-01" if batch == 15 else "2035-05-03"
                item = dict(op="add", kind="event", category="other", evidence=[ref], event_date=date,
                            content=f"They planned a museum visit for {date}.")
            if previous:
                self.assertIn(previous.id, data)
                item.update(fact_id=previous.id, expected_version=previous.record_version)
            yield json.dumps([item])

        with patch("src.llm.registry.get_provider", return_value=SimpleNamespace(stream_chat=stream)):
            for batch in range(20):
                for turn in range(30):
                    await self.turn(triggers.get(batch, "Good morning.") if turn == 0 else "Nice to see you.")
        self.assertEqual(len(self.calls), 20)
        facts = self.manager.facts.get_all()
        self.assertEqual(len(facts), 4)
        experience = next(f for f in facts if f.kind == "experience")
        preference = next(f for f in facts if f.category == "preference")
        self.assertEqual(len(experience.versions), 2)
        self.assertEqual(len(experience.evidence), 3)
        self.assertEqual(experience.content, " ".join(story))
        self.assertIsNone(experience.expires_at)
        self.assertIsNone(experience.occurred_from)
        self.assertEqual(preference.versions[0]["content"], "They prefer unsweetened tea.")
        self.assertEqual(preference.content, "They now prefer tea with honey.")
        self.assertEqual({f.event_date for f in facts if f.kind == "event"}, {"2035-05-01", "2035-05-03"})
        self.assertNotIn("They prefer unsweetened tea.", self.chat_input("What do I like in my tea?"))
        self.assertIn("They now prefer tea with honey.", self.chat_input("What do I like in my tea?"))

        dialogue_day = datetime.fromtimestamp(experience.evidence[0]["timestamp"]).date().isoformat()
        self.manager.day_store.add_summary(dialogue_day, experience.content + " Also checked the weather.")
        combined = self.chat_input("Remember the sunrise at the pier?")
        self.assertEqual(combined.count(experience.content), 1)
        self.assertIn("Also checked the weather.", combined)
        self.manager.day_store.delete(dialogue_day)

        service = MemoryCuration("fixture", self.manager.facts)
        async def decide(rows):
            return [dict(fact_id=f.id, action="archive" if f.id == experience.id else "keep",
                         reason="A meaningful memory for relevant topics", archive_reason="cold_memory")
                    for f in rows]
        preview = await service.preview(decide)
        service.apply(preview["preview_id"])
        archived = self.manager.facts.get_by_id(experience.id)
        self.assertTrue(archived.archived)
        self.assertNotIn("missed the ferry", self.chat_input("What should I make for dinner?"))
        self.assertIn(experience.content, self.chat_input("Remember the sunrise at the pier?"))

        # A semantic-only query exercises the actual admission and prompt path with an offline index stub.
        self.manager.vectors = SimpleNamespace(is_available=lambda: True, search=lambda *_args, **_kwargs: [{
            "metadata": {"fact_id": archived.id}, "document": _vector_document_for_fact(archived), "distance": 0.08,
        }])
        self.assertIn(experience.content, self.chat_input("That unexpectedly beautiful dawn by the waterfront"))
        service.restore(archived.id, archived.archive_revision)
        self.assertFalse(self.manager.facts.get_by_id(experience.id).archived)

    async def test_long_user_message_can_resume_after_manager_restart(self):
        self.manager = self.new_manager(1)
        self.app.state.memory_managers["fixture"] = self.manager
        marker = "Important correction at the end: tea with mint."
        refs = []

        async def stream(messages):
            self.calls.append(messages)
            data = messages[-1]["content"]
            if marker not in data:
                yield "[]"
                return
            line = next(line for line in data.splitlines() if marker in line)
            refs.append(re.match(r"\[([^]]+)\]", line).group(1))
            yield json.dumps([dict(op="add", kind="stable", category="preference",
                                   content="They prefer tea with mint.", evidence=refs)])

        with patch("src.llm.registry.get_provider", return_value=SimpleNamespace(stream_chat=stream)):
            original = await self.turn("a" * 35_000 + marker)
            _, cursors, _, offsets = self.manager.facts.extraction_progress_snapshot()
            self.assertLess(cursors["single"], original)
            self.assertEqual(offsets["single"]["offset"], 32000)
            self.assertEqual(len(self.manager.facts.get_all()), 0)
            self.manager = self.new_manager(1)
            await self.turn("Hello again.")
        fact = self.manager.facts.get_all()[0]
        self.assertEqual(fact.evidence[0]["id"], original)
        self.assertEqual(fact.evidence[0]["start_char"], 32000)
        self.assertTrue(refs[0].startswith(f"single:{original}#32000-"))
        self.assertEqual(self.manager.facts.source_cursors()["single"], self.store.memory_tail_id())
        self.assertFalse(self.manager.facts.extraction_progress_snapshot()[-1])
