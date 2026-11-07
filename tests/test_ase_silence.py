import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from src.core.ase import AseEngine, could_be_llm_error, could_become_silence_reply, is_silence_reply
from src.prompt.segments.ase.scene_context import AseSceneContextSegment

SILENCE_REPLIES = (
    "静默", "（静默）", "(静默)", "[静默]", "【静默】",
    "silence", "(silence)", "[silence]", "（silence）",
    "SILENCE", "(Silence)", "  （静默）\n", "「（静默）」", "“(silence)”", "\"(silence)\"",
    "'silence'", "（静默）。", "(silence).", "silence...", "静默……",
    "**（静默）**", "*(silence)*", "( silence )", "（静默)",
)

SPOKEN_REPLIES = (
    "", "   ", "……", "早上好",
    "（静默）你还在吗？", "(silence) Are you there?", "（静默）\n嗯，早。",
    "我静默了一会儿", "静默中", "（静默片刻）", "静默静默", "(silence)(silence)",
    "silence is golden", "The silence here is nice.", "silent", "（沉默）",
)


class IsSilenceReplyTests(unittest.TestCase):
    def test_bare_silence_markers_match(self):
        for text in SILENCE_REPLIES:
            with self.subTest(text=text):
                self.assertTrue(is_silence_reply(text))

    def test_replies_with_other_content_do_not_match(self):
        for text in SPOKEN_REPLIES:
            with self.subTest(text=text):
                self.assertFalse(is_silence_reply(text))


class CouldBecomeSilenceReplyTests(unittest.TestCase):
    def test_every_prefix_of_a_silence_reply_is_held_back(self):
        for text in SILENCE_REPLIES:
            for end in range(len(text) + 1):
                with self.subTest(prefix=text[:end]):
                    self.assertTrue(could_become_silence_reply(text[:end]))

    def test_ordinary_replies_are_released_early(self):
        for prefix in ("早", "So", "（揉", "*st", "[LLM Error", "silent", "静默中", "(silence) A"):
            with self.subTest(prefix=prefix):
                self.assertFalse(could_become_silence_reply(prefix))


class CouldBeLlmErrorTests(unittest.TestCase):
    def test_error_string_and_its_prefixes_are_held_back(self):
        text = "[LLM Error: Connection timed out]"
        for end in range(len(text) + 1):
            with self.subTest(prefix=text[:end]):
                self.assertTrue(could_be_llm_error(text[:end]))

    def test_ordinary_replies_are_not_errors(self):
        for prefix in ("早", "Sure", "[smiles]", "[LL M", "(silence)"):
            with self.subTest(prefix=prefix):
                self.assertFalse(could_be_llm_error(prefix))


class ProactiveTopicSceneTests(unittest.TestCase):
    def test_local_topic_is_optional_in_both_languages(self):
        ctx = SimpleNamespace(session=SimpleNamespace(), extras={"ase_chosen_topic": {
            "source_id": "random_api",
            "material": "给月亮寄一张明信片，你会画什么？",
            "angle": "上一轮自省拟好的开场",
            "framing_hint": "用「突然想问你」开场",
        }})
        for locale, optional, committed, recent, silence in (
            ("zh", "【可选的本地灵感】", "【你想说的事】", "最近的对话", "（静默）"),
            ("en", "[Optional Local Inspiration]", "[What You Want to Say]", "recent conversation", "(silence)"),
        ):
            with self.subTest(locale=locale), mock.patch(
                "src.prompt.segments.ase.scene_context.get_locale", return_value=locale
            ):
                content = AseSceneContextSegment().build(ctx).messages[0]["content"]
                self.assertIn(optional, content)
                self.assertIn("给月亮寄一张明信片，你会画什么？", content)
                self.assertIn(recent, content)
                self.assertIn(silence, content)
                self.assertNotIn(committed, content)
                self.assertNotIn("上一轮自省拟好的开场", content)
                self.assertNotIn("用「突然想问你」开场", content)


class _FakeProvider:
    def __init__(self, tokens):
        self._tokens = tokens

    async def stream_chat(self, messages):
        for token in self._tokens:
            yield token


class SpeakSilenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.storage_root = tmp.name
        self.pushed = []
        self.notices = []
        self.stored = []
        self.stored_ids = []
        self._next_message_id = 41
        config = SimpleNamespace(
            get_ase_config=lambda: {},
            get_vlm_config=lambda: {},
            get_active_llm_preset=lambda: {"api_key": "test", "model": "test-model"},
        )
        self.engine = AseEngine(SimpleNamespace(state=SimpleNamespace(config=config, last_user_message_time=0.0)))
        self.session = SimpleNamespace(
            id="p1",
            profile_id="p1",
            display_name="A",
            storage_root=self.storage_root,
            last_user_message_time=0.0,
            conversation_store=SimpleNamespace(
                append=self._append_message
            ),
            reflection_state={"urgency": 0.9, "thought": "-"},
            save_runtime_state=lambda: None,
        )

    def _append_message(self, role, content, sender="", **metadata):
        """Like ConversationStore.append, persist a row and return its integer ID."""
        message_id = self._next_message_id
        self._next_message_id += 1
        self.stored.append((role, content))
        self.stored_ids.append(message_id)
        return message_id

    async def _speak(self, tokens=(), *, provider=None, build=None):
        async def push(session_id, data, exclude=None):
            self.pushed.append(data)

        async def push_all(data):
            self.notices.append(data)

        mode_cfg = {"message_config": {"n_history_turns": 4, "extra_behavior": "-"}}
        with mock.patch("src.core.broadcast.broadcast", SimpleNamespace(push=push, push_all=push_all)), \
                mock.patch("src.prompt.pipeline.build_messages", side_effect=build,
                           return_value=[{"role": "system", "content": "Full proactive context"}]), \
                mock.patch("src.llm.registry.get_provider", return_value=provider or _FakeProvider(tokens)), \
                mock.patch("src.api.memory.get_or_create_manager_for_session"), \
                mock.patch("src.core.ase._get_reflection_config_for_profile", return_value={}), \
                mock.patch("src.core.ase._compute_ase_segment_injections", return_value=([], [])), \
                mock.patch.multiple(
                    "src.utils.debug_logger",
                    log_llm_call=mock.DEFAULT, log_llm_response=mock.DEFAULT, log_ase_check=mock.DEFAULT,
                    log_ase_speak=mock.DEFAULT, log_ase_error=mock.DEFAULT,
                ):
            await self.engine._speak(self.session, self.session.reflection_state, "medium", mode_cfg)

    async def test_proactive_chat_selects_query_and_writes_only_the_grounded_final_reply(self):
        runtime = SimpleNamespace(
            autonomy=SimpleNamespace(research_enabled=lambda _: True, research=mock.AsyncMock(
                return_value={"run_id":"web-run", "summary":"An exhibit opened.",
                              "sources":[{"title":"Aquarium exhibit","url":"https://example.test/a",
                                          "snippet":"A new aquarium exhibit opened today."}]})),
            store=SimpleNamespace(bind_messages=mock.Mock()),
        )
        self.engine._app.state.agent_runtime = runtime
        calls = []
        spoken = "发现了个新水族展。[展览介绍](https://example.test/a)"

        class Provider:
            async def stream_chat(_self, messages):
                calls.append(messages)
                if len(calls) == 1:
                    yield '<shikigami-task>{"goal":"new public aquarium exhibits"}</shikigami-task>'
                else:
                    yield spoken

        await self._speak(provider=Provider())
        self.assertEqual(len(calls),2)
        self.assertIn("Full proactive context",calls[0][0]["content"])
        self.assertIn("https://example.test/a",calls[1][0]["content"])
        runtime.autonomy.research.assert_awaited_once()
        self.assertEqual(runtime.autonomy.research.call_args.args[1],"new public aquarium exhibits")
        self.assertEqual(self.stored,[("assistant",spoken)])
        self.assertEqual(self._streamed_text(),spoken)
        runtime.store.bind_messages.assert_called_once_with("web-run",assistant_message_id=41)
        self.assertEqual(self.engine._state["p1"]["proactive_log"][-1]["source"],"web_research")

    async def test_proactive_web_search_failure_does_not_turn_into_a_fabricated_topic(self):
        self.engine._app.state.agent_runtime = SimpleNamespace(
            autonomy=SimpleNamespace(research_enabled=lambda _:True,research=mock.AsyncMock(return_value=None)))
        await self._speak(['<shikigami-task>{"goal":"public aquarium exhibits"}</shikigami-task>'])
        self.assertEqual(self.stored,[])
        self.assertEqual(self._streamed_text(),"")

    async def test_proactive_search_rejects_private_queries_before_harness(self):
        research = mock.AsyncMock()
        self.engine._app.state.agent_runtime = SimpleNamespace(
            autonomy=SimpleNamespace(research_enabled=lambda _:True,research=research))
        for query in ("查一下手机号13812345678", "Morgan Example 42 Oak Lane"):
            await self._speak(['<shikigami-task>{"goal":"' + query + '"}</shikigami-task>'])
        research.assert_not_awaited()
        self.assertEqual(self.stored,[])

    async def test_researched_message_with_a_price_absent_from_sources_is_not_sent(self):
        self.engine._app.state.agent_runtime = SimpleNamespace(
            autonomy=SimpleNamespace(research_enabled=lambda _:True,research=mock.AsyncMock(
                return_value={"run_id":"fixture","sources":[{"title":"Aquarium","url":"https://example.test/a",
                                                            "snippet":"An exhibit has opened."}]})))
        class Provider:
            count = 0
            async def stream_chat(inner, messages):
                inner.count += 1
                yield ('<shikigami-task>{"goal":"new aquarium exhibits"}</shikigami-task>' if inner.count==1
                       else "票价30元。[展览](https://example.test/a)")
        await self._speak(provider=Provider())
        self.assertEqual(self.stored,[])
        self.assertEqual(self._streamed_text(),"")

    async def test_history_revision_change_discards_a_partial_proactive_bubble(self):
        revision = 0
        self.engine._app.state.agent_runtime = SimpleNamespace(store=SimpleNamespace(revision=lambda _:revision))
        class Provider:
            async def stream_chat(inner, messages):
                nonlocal revision
                yield "正在生成的消息"
                revision += 1
                yield "已经过时的尾段"
        await self._speak(provider=Provider())
        self.assertEqual(self.stored,[])
        self.assertTrue(any(p.get("type")=="proactive_stream_discard" for p in self.pushed))

    async def test_history_mutation_barrier_discards_before_revision_is_written(self):
        busy = False
        self.engine._app.state.agent_runtime = SimpleNamespace(
            store=SimpleNamespace(revision=lambda _:0), history_changing=lambda _:busy)
        class Provider:
            async def stream_chat(inner, messages):
                nonlocal busy
                yield "正在生成的消息"
                busy = True
                yield "不要发布"
        await self._speak(provider=Provider())
        self.assertEqual(self.stored,[])
        self.assertTrue(any(p.get("type")=="proactive_stream_discard" for p in self.pushed))

    async def test_web_enabled_does_not_force_an_extra_request_on_normal_proactive_speech(self):
        research = mock.AsyncMock()
        self.engine._app.state.agent_runtime = SimpleNamespace(
            autonomy=SimpleNamespace(research_enabled=lambda _:True,research=research))
        await self._speak(["今天想和你聊聊音乐。"])
        research.assert_not_awaited()
        self.assertEqual(self.stored,[("assistant","今天想和你聊聊音乐。")])

    async def test_old_story_uses_original_user_text_and_is_excluded_after_speaking(self):
        import time
        from src.core.topics.base import TopicSourceContext
        from src.core.topics.sources.conversation_recall import ConversationRecallTopicSource
        from src.memory.conversation_store import ConversationStore

        history = ConversationStore(self.storage_root, max_turns=0)
        self.addCleanup(history.close)
        self.session._store = history
        event = history.append("user","我修好了阳台门。")
        countdown = history.append("user","我订好了新住处，酒店还剩三天。")
        fabricated = history.append("assistant","你终于修好了阳台门。")
        with history._conn:
            history._conn.execute("UPDATE messages SET timestamp=? WHERE id IN (?,?,?)",
                                  (time.time()-10*86400,event,countdown,fabricated))
        source = ConversationRecallTopicSource()
        candidates = source.get_candidates(TopicSourceContext("p1", self.storage_root, session=self.session))
        self.assertEqual(len(candidates), 1)
        self.session.reflection_state["chosen_topic"] = {
            "source":"conversation_recall","item_id":candidates[0].item_id,"picked_at":time.time(),
        }

        def prompt(*_args, **kwargs):
            material = kwargs["extra_context"]["ase_chosen_topic"]["material"]
            self.assertIn("我修好了阳台门。", material)
            self.assertNotIn("酒店还剩三天", material)
            return [{"role":"system","content":material}]

        await self._speak(["想起你前阵子说修好了阳台门，现在开关顺手吗？"],build=prompt)
        self.assertEqual(len(self.stored),1)
        self.assertIn(candidates[0].item_id,self.engine._state["p1"]["recall_spoken"])
        self.assertIsNone(self.engine._resolve_chosen_topic(self.session))

    def _streamed_text(self):
        return "".join(p["token"] for p in self.pushed if "token" in p and not p["done"])

    async def test_silence_reply_is_not_sent_stored_or_counted(self):
        await self._speak(["「（静", "默）", "」\n"])

        self.assertEqual([], self.pushed)
        self.assertEqual([], self.stored)
        self.assertEqual([], self.notices)
        self.assertEqual({}, self.engine._state.get("p1", {}))
        self.assertFalse(os.path.exists(os.path.join(self.storage_root, "ase_state.json")))
        self.assertEqual(0.0, self.session.reflection_state["urgency"])

    async def test_empty_reply_is_not_sent_or_stored(self):
        await self._speak([" ", "\n"])

        self.assertEqual([], self.pushed)
        self.assertEqual([], self.stored)

    async def test_ordinary_reply_is_streamed_stored_and_counted(self):
        await self._speak(["S", "ure", ", see you", " later."])

        self.assertEqual("Sure", self.pushed[0]["token"])
        self.assertEqual("Sure, see you later.", self._streamed_text())
        self.assertEqual([("assistant", "Sure, see you later.")], self.stored)
        self.assertEqual(1, len(self.engine._state["p1"]["timestamps_24h"]))
        self.assertEqual(1, self.engine._state["p1"]["consecutive_speaks"])
        self.assertTrue(self.pushed[-1]["done"])

    async def test_llm_error_is_not_sent_stored_or_counted(self):
        await self._speak(["[LLM", " Error: 500 Internal Server Error]"])

        self.assertEqual([], self.pushed)
        self.assertEqual([], self.stored)
        self.assertEqual({}, self.engine._state.get("p1", {}))
        self.assertEqual(0.0, self.session.reflection_state["urgency"])

    async def test_error_after_partial_speech_discards_the_stream_without_a_notice(self):
        await self._speak(["Hello", " there.", "[LLM Error: disconnect]"])
        self.assertEqual(self.stored, [])
        self.assertEqual(self.notices, [])
        self.assertEqual(self.pushed[-1], {"type": "proactive_stream_discard"})

    async def test_broadcast_uses_stored_ids_for_streamed_and_held_replies(self):
        await self._speak(["Hello", " there."])
        await self._speak(["(", "sil"])

        messages = [p for p in self.pushed if p.get("type") == "new_message"]
        self.assertEqual(2, len(messages))
        self.assertEqual([41, 42], self.stored_ids)
        self.assertEqual(self.stored_ids, [p.get("message_id") for p in messages])
        self.assertEqual(self.stored, [(p["role"], p["content"]) for p in messages])
        completed = [p for p in self.pushed if "token" in p and p.get("done")]
        self.assertEqual(self.stored_ids, [p.get("message_id") for p in completed])
        self.assertEqual(self.stored_ids, [p["message_id"] for p in self.notices])
        self.assertEqual(self.session.last_proactive_message["message_id"], 42)
        self.assertTrue(all(p["type"] == "proactive_message" and p["session_id"] == "p1"
                            and "content" not in p for p in self.notices))

    async def test_proactive_notice_survives_restart_and_clears_when_the_message_is_deleted(self):
        from src.api.sessions import _prune_proactive_message
        from src.core.session import Session

        session = Session("a", "a", "A", self.storage_root)
        store = session.conversation_store
        self.addCleanup(store.close)
        message_id = store.append("assistant", "A proactive hello")
        session.last_proactive_message = {"message_id": message_id, "timestamp": 123.0}
        self.assertTrue(session.save_runtime_state())
        reopened = Session("a", "a", "A", self.storage_root)
        reopened.load_runtime_state()
        self.assertEqual(reopened.last_proactive_message, session.last_proactive_message)
        with mock.patch("src.api.sessions.broadcast.push_all", new_callable=mock.AsyncMock) as push:
            await _prune_proactive_message(reopened, store)
            push.assert_not_awaited()
            self.assertTrue(store.delete_by_id(message_id))
            await _prune_proactive_message(reopened, store)
            push.assert_awaited_once_with({"type": "proactive_message", "session_id": "a",
                                          "removed": True, "message_id": message_id, "timestamp": 123.0})
        final = Session("a", "a", "A", self.storage_root)
        final.load_runtime_state()
        self.assertIsNone(final.last_proactive_message)

    async def test_reply_held_back_until_the_end_is_still_sent(self):
        await self._speak(["(", "sil"])

        self.assertEqual("(sil", self._streamed_text())
        self.assertEqual([("assistant", "(sil")], self.stored)
        self.assertTrue(self.pushed[-1]["done"])


if __name__ == "__main__":
    unittest.main()
