from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.memory.conversation_store import ConversationStore
from src.memory.memory_manager import MemoryManager
from src.memory.long_term_store import LongTermStore


class MemoryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temp)
        self.chat = ConversationStore(str(self.root / 'chat'), max_turns=0)
        self.addCleanup(self.chat.close)
        self.manager = MemoryManager("fixture", str(self.root / "memory"),
            profile_memory_cfg={"enabled": True, "extraction_frequency": 1, "vector_enabled": False})
        self.sources = {"single": self.chat}
        self.app = SimpleNamespace(state=SimpleNamespace(config=SimpleNamespace(user_name="用户")))
        self.model = self.enterContext(patch.object(self.manager, "_extract_facts_llm", new_callable=AsyncMock))
        await self.manager.auto_extract(self.sources, {}, self.app)

    async def turn(self, user_text, change):
        mid = self.chat.append("user", user_text)
        self.chat.append("assistant", "收到")
        self.model.return_value = [{**change, "evidence": [f"single:{mid}"]}]
        await self.manager.auto_extract(self.sources, {}, self.app)
        return mid

    async def test_repeated_confirmation_and_correction(self):
        await self.turn("我喜欢称呼你小雨", {"op": "add", "kind": "address", "content": "用户喜欢称呼AI为小雨"})
        fact = self.manager.facts.get_all()[0]
        for index in range(10):
            await self.turn(f"小雨，我还是这么叫你。{index}", {"op": "confirm", "fact_id": fact.id})
        await self.turn("今后我想称呼你小雪", {"op": "update", "fact_id": fact.id,
                                             "kind": "address", "content": "用户现在喜欢称呼AI为小雪"})
        restored = LongTermStore(str(self.root / "memory"))
        self.assertEqual(restored.count(), 1)
        current = restored.get_active()[0]
        self.assertEqual(current.id, fact.id)
        self.assertIn("小雪", current.content)
        self.assertIn("小雨", current.versions[0]["content"])

    async def test_expiry_on_read_and_reflection_without_maintenance(self):
        await self.turn("2026-11-10有二面", {"op": "add", "kind": "event",
            "content": "用户计划于2026-11-10参加二面", "event_date": "2026-11-10", "category": "milestone"})
        fact = self.manager.facts.get_all()[0]
        self.assertEqual(len(self.manager.facts.get_active(now=fact.expires_at - 1)), 1)
        self.assertEqual(self.manager.facts.get_active(now=fact.expires_at), [])
        with patch("src.memory.long_term_store.time.time", return_value=fact.expires_at):
            self.assertEqual(self.manager.get_reflection_context_facts(), [])
        self.assertEqual(self.manager.facts.count(), 1)

    async def test_failed_atomic_save_preserves_cursor_and_retries_once(self):
        with patch("src.memory.long_term_store.os.replace", side_effect=OSError("fixture")):
            await self.turn("我喝茶不加糖", {"op": "add", "content": "用户喝茶时不加糖", "kind": "stable"})
        self.assertEqual(self.manager.facts.count(), 0)
        await self.manager.auto_extract(self.sources, {}, self.app)
        self.assertEqual(self.manager.facts.count(), 1)
        calls = self.model.await_count
        await self.manager.auto_extract(self.sources, {}, self.app)
        self.assertEqual(self.model.await_count, calls)

    async def test_active_facts_only_enter_chat_when_related(self):
        from src.prompt.base import BuildContext
        from src.prompt.segments.long_term_facts import LongTermFactsSegment

        await self.turn("喝茶的时候我不加糖", {"op": "add", "content": "用户喝茶不加糖", "kind": "stable"})
        await self.turn("2026-11-10有二面", {"op": "add", "content": "用户计划参加二面",
                        "kind": "event", "event_date": "2026-11-10"})
        fact = self.manager.facts.get_all()[-1]
        self.app.state.memory_managers = {"fixture": self.manager}
        session = SimpleNamespace(profile_id="fixture")
        def recall(text):
            return LongTermFactsSegment().build(BuildContext(
                session, {}, self.chat, text, extras={"app": self.app}))
        with patch("time.time", return_value=fact.expires_at - 1):
            self.assertIn("2026-11-10", recall("我的二面").messages[0]["content"])
            self.assertFalse(recall("推荐一部电影").fired)
        with patch("time.time", return_value=fact.expires_at):
            self.assertFalse(recall("我的二面").fired)
            self.assertIn("喝茶不加糖", recall("喝茶放什么").messages[0]["content"])

    async def test_chat_and_reflection_read_same_current_address(self):
        from src.prompt.base import BuildContext, ReflectionBuildContext
        from src.prompt.segments.reflection.user_persona import ReflectionUserPersonaSegment
        from src.prompt.segments.user_persona import UserPersonaSegment

        await self.turn("我会叫你小雨", {"op": "add", "kind": "address", "content": "用户喜欢称呼AI为小雨"})
        fid = self.manager.facts.get_all()[0].id
        await self.turn("今后我想称呼你小雪", {"op": "update", "kind": "address", "fact_id": fid,
                        "content": "用户喜欢称呼AI为小雪"})
        self.app.state.memory_managers = {"fixture": self.manager}
        session = SimpleNamespace(profile_id="fixture")
        chat_ctx = BuildContext(session, {}, self.chat, "你好", extras={"app": self.app})
        reflection_ctx = ReflectionBuildContext("fixture", {}, [], None, None, 0, "", "", None,
                                               None, [], None, None, memory_manager=self.manager)
        for segment, context in ((UserPersonaSegment(), chat_ctx), (ReflectionUserPersonaSegment(), reflection_ctx)):
            text = segment.build(context).messages[0]["content"]
            self.assertIn("小雪", text)
            self.assertNotIn("小雨", text)

    async def test_model_cannot_use_assistant_evidence_or_overwrite_a_manual_edit(self):
        user_id = self.chat.append("user", "我喜欢泡茶")
        assistant_id = self.chat.append("assistant", "你每天喝两升茶")
        self.model.return_value = [{"op": "add", "content": "用户每天喝两升茶", "evidence": [f"single:{assistant_id}"]}]
        await self.manager.auto_extract(self.sources, {}, self.app)
        self.assertEqual(self.manager.facts.count(), 0)
        user_id = self.chat.append("user", "我确实喜欢泡茶")
        before = self.manager.facts.source_cursors()

        async def conflicting_edit(*args, **kwargs):
            self.manager.facts.add("用户手动保存的备注")
            return [{"op": "add", "content": "用户喜欢泡茶", "evidence": [f"single:{user_id}"]}]

        self.model.side_effect = conflicting_edit
        await self.manager.auto_extract(self.sources, {}, self.app)
        self.assertEqual(self.manager.facts.count(), 1)
        self.assertEqual(self.manager.facts.source_cursors(), before)

    async def test_bootstrap_reads_current_user_turn_without_reprocessing_old_history(self):
        other = ConversationStore(str(self.root / "other"), max_turns=0)
        self.addCleanup(other.close)
        other.append("user", "以前的聊天")
        mid = other.append("user", "今天的第一轮")
        self.model.return_value = []
        await self.manager.auto_extract({"group:fixture": other}, {}, self.app,
                                       first_message=("group:fixture", mid))
        rows = self.model.call_args.args[0]
        self.assertEqual([(row["source"], row["id"]) for row in rows], [("group:fixture", mid)])

    async def test_profile_settings_reload_cannot_lose_another_instances_edit(self):
        mid = self.chat.append("user", "我喜欢泡茶")
        cursor = self.manager.facts.source_cursors()

        async def concurrent_replacement(*args, **kwargs):
            replacement = LongTermStore(str(self.root / "memory"))
            replacement.add("用户手动保存的备注")
            return [{"op": "add", "content": "用户喜欢泡茶", "evidence": [f"single:{mid}"]}]

        self.model.side_effect = concurrent_replacement
        await self.manager.auto_extract(self.sources, {}, self.app)
        current = LongTermStore(str(self.root / "memory"))
        self.assertEqual(current.source_cursors(), cursor)
        self.assertEqual([fact.content for fact in current.get_all()], ["用户手动保存的备注"])

    async def test_nonmerged_single_and_group_cadences_each_get_a_turn(self):
        self.manager._cfg["extraction_frequency"] = 2
        self.model.return_value = []
        self.model.reset_mock()
        group = ConversationStore(str(self.root / "group"), max_turns=0)
        self.addCleanup(group.close)
        for _ in range(4):
            for source, store in (("single", self.chat), ("group:fixture", group)):
                mid = store.append("user", "a valid prompt", sender="Group nickname")
                store.append("assistant", "a complete reply")
                await self.manager.auto_extract({source: store}, {}, self.app,
                                               first_message=(source, mid))
        self.assertEqual(set(self.manager.facts.source_cursors()), {"single", "group:fixture"})
        self.assertGreater(self.manager.facts.source_cursors()["single"], 0)
        self.assertEqual(self.manager.facts.source_cursors()["group:fixture"], group.memory_tail_id())
        self.assertEqual(self.model.await_count, 4)

    async def test_explicitly_rescheduled_event_preserves_the_old_date_in_history(self):
        from datetime import date, timedelta

        old_date = (date.today() + timedelta(days=10)).isoformat()
        new_date = (date.today() + timedelta(days=12)).isoformat()
        await self.turn(f"{old_date}有二面", {"op": "add", "kind": "event",
                        "content": "用户计划参加二面", "event_date": old_date})
        fid = self.manager.facts.get_all()[0].id
        await self.turn(f"二面由{old_date}改到{new_date}", {"op": "update", "kind": "event",
                        "fact_id": fid, "content": "用户计划参加二面", "event_date": new_date})
        facts = self.manager.facts.get_active()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].event_date, new_date)
        self.assertEqual(facts[0].versions[0]["event_date"], old_date)

    async def test_legacy_auto_address_is_preserved_and_current_address_wins_recall(self):
        old = self.manager.facts.add("用户喜欢称呼AI为小雨", category="preference", source="auto")
        await self.turn("我之后会叫你小雪", {"op": "add", "kind": "address",
                        "content": "用户喜欢称呼AI为小雪"})
        self.assertEqual(self.manager.facts.count(), 2)
        self.assertIsNotNone(self.manager.facts.get_by_id(old.id))
        self.assertEqual([fact.content for fact in self.manager.facts.get_recallable()],
                         ["用户喜欢称呼AI为小雪"])

    async def test_chinese_preference_paraphrases_find_the_existing_candidate(self):
        mid = await self.turn("我喝茶不加糖", {"op": "add", "kind": "stable",
                              "category": "preference", "content": "用户喝茶不加糖"})
        fid = self.manager.facts.get_all()[0].id
        for phrase in ("我喝茶不爱加糖", "泡茶给我别加糖", "喝茶时不要额外放糖", "喝茶还是原味的",
                       "喝茶保持无糖", "不加糖的茶我喜欢", "茶里不要加糖", "我想喝茶且不放糖",
                       "倒茶后就不用加糖", "我的茶不加糖"):
            await self.turn(phrase, {"op": "confirm", "fact_id": fid})
            self.assertIn(fid, [fact.id for fact in self.model.call_args.kwargs["candidates"]])
        facts = self.manager.facts.get_all()
        self.assertEqual(len(facts), 1)
        self.assertEqual(len(facts[0].evidence), 11)
        self.assertEqual(facts[0].evidence[0]["id"], mid)

    async def test_replacement_manager_refreshes_after_an_older_writer_commits(self):
        replacement = LongTermStore(str(self.root / "memory"))
        await self.turn("我喝茶不加糖", {"op": "add", "kind": "stable", "content": "用户喝茶不加糖"})
        self.assertEqual(replacement.count(), 1)
        self.assertIsNotNone(replacement.add("用户手工记录的事实"))
        self.assertEqual(self.manager.facts.count(), 2)

    async def test_other_source_first_message_is_not_consumed_before_its_own_reply(self):
        group = ConversationStore(str(self.root / "another-group"), max_turns=0)
        self.addCleanup(group.close)
        group_mid = group.append("user", "群聊第一条正在等待回复")
        single_mid = self.chat.append("user", "单聊消息")
        self.model.return_value = []
        sources = {"single": self.chat, "group:g": group}
        await self.manager.auto_extract(sources, {}, self.app, first_message=("single", single_mid))
        self.assertNotIn("group:g", self.manager.facts.source_cursors())
        group.append("assistant", "群聊回复")
        await self.manager.auto_extract(sources, {}, self.app, first_message=("group:g", group_mid))
        self.assertIn(("group:g", group_mid), [(row["source"], row["id"]) for row in self.model.call_args.args[0]])

    async def test_thirty_turn_cadence_processes_all_sixty_messages(self):
        self.manager._cfg["extraction_frequency"] = 30
        Path(self.manager._meta_path).write_text('{"extraction_turns":{"single":0}}', encoding="utf-8")
        self.model.return_value = []
        self.model.reset_mock()
        for i in range(30):
            mid = self.chat.append("user", f"normal user message {i}")
            self.chat.append("assistant", "completed reply")
            await self.manager.auto_extract(self.sources, {}, self.app, first_message=("single", mid))
        self.assertEqual(self.manager.facts.source_cursors()["single"], self.chat.memory_tail_id())
        self.assertEqual(len(self.model.call_args.args[0]), 60)
        self.assertEqual(self.model.await_count, 1)

    async def test_group_cadence_uses_complete_turns_with_multiple_assistants(self):
        self.manager._cfg["extraction_frequency"] = 30
        self.model.return_value = []
        self.model.reset_mock()
        group = ConversationStore(str(self.root / "large-group"), max_turns=0)
        self.addCleanup(group.close)
        sources = {**self.sources, "group:large": group}
        for i in range(30):
            mid = group.append("user", f"group user message {i}")
            for sender in ("A", "B", "C"):
                group.append("assistant", "completed group reply", sender=sender)
            await self.manager.auto_extract(sources, {}, self.app, first_message=("group:large", mid))
        self.assertEqual(self.manager.facts.source_cursors()["group:large"], group.memory_tail_id())
        self.assertEqual(len(self.model.call_args.args[0]), 120)
        self.assertEqual(self.model.await_count, 1)


    async def test_one_confirmation_can_cite_ten_valid_user_messages(self):
        await self.turn("我喝茶不加糖", {"op": "add", "kind": "stable", "content": "用户喝茶不加糖"})
        fid = self.manager.facts.get_all()[0].id
        self.manager._cfg["extraction_frequency"] = 10
        Path(self.manager._meta_path).write_text('{"extraction_turns":{"single":0}}', encoding="utf-8")
        refs = []
        for i in range(10):
            mid = self.chat.append("user", f"喝茶仍然不加糖，第{i+1}次确认")
            self.chat.append("assistant", "收到")
            refs.append(f"single:{mid}")
            self.model.return_value = [{"op": "confirm", "fact_id": fid, "evidence": refs.copy()}]
            await self.manager.auto_extract(self.sources, {}, self.app, first_message=("single", mid))
        self.assertEqual(self.manager.facts.source_cursors()["single"], self.chat.memory_tail_id())
        fact = self.manager.facts.get_by_id(fid)
        self.assertEqual(len(fact.evidence), 11)
        self.assertEqual(self.manager.facts.count(), 1)
