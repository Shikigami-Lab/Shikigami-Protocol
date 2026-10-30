import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from src.core.ase import AseEngine, could_be_llm_error, could_become_silence_reply, is_silence_reply

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

    def _append_message(self, role, content, sender=""):
        """Like ConversationStore.append, persist a row and return its integer ID."""
        message_id = self._next_message_id
        self._next_message_id += 1
        self.stored.append((role, content))
        self.stored_ids.append(message_id)
        return message_id

    async def _speak(self, tokens):
        async def push(session_id, data, exclude=None):
            self.pushed.append(data)

        mode_cfg = {"message_config": {"n_history_turns": 4, "extra_behavior": "-"}}
        with mock.patch("src.core.broadcast.broadcast", SimpleNamespace(push=push)), \
                mock.patch("src.prompt.pipeline.build_messages", return_value=[{"role": "system", "content": "-"}]), \
                mock.patch("src.llm.registry.get_provider", return_value=_FakeProvider(tokens)), \
                mock.patch("src.api.memory.get_or_create_manager_for_session"), \
                mock.patch("src.core.ase._get_reflection_config_for_profile", return_value={}), \
                mock.patch("src.core.ase._compute_ase_segment_injections", return_value=([], [])), \
                mock.patch.multiple(
                    "src.utils.debug_logger",
                    log_llm_call=mock.DEFAULT, log_llm_response=mock.DEFAULT, log_ase_check=mock.DEFAULT,
                    log_ase_speak=mock.DEFAULT, log_ase_error=mock.DEFAULT,
                ):
            await self.engine._speak(self.session, self.session.reflection_state, "medium", mode_cfg)

    def _streamed_text(self):
        return "".join(p["token"] for p in self.pushed if "token" in p and not p["done"])

    async def test_silence_reply_is_not_sent_stored_or_counted(self):
        await self._speak(["「（静", "默）", "」\n"])

        self.assertEqual([], self.pushed)
        self.assertEqual([], self.stored)
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

    async def test_reply_held_back_until_the_end_is_still_sent(self):
        await self._speak(["(", "sil"])

        self.assertEqual("(sil", self._streamed_text())
        self.assertEqual([("assistant", "(sil")], self.stored)
        self.assertTrue(self.pushed[-1]["done"])


if __name__ == "__main__":
    unittest.main()
