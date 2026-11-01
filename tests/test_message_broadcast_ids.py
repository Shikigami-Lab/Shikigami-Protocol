import json
import shutil
import subprocess
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.api.chat import SystemTriggerRequest, system_trigger_endpoint
from src.tools.timer.expire_handler import _on_expire


class _FakeStore:
    def __init__(self):
        self.rows = []
        self.next_id = 101

    def append(self, role, content, sender=""):
        message_id = self.next_id
        self.next_id += 1
        self.rows.append(dict(id=message_id, role=role, content=content, sender=sender))
        return message_id


class _FakeProvider:
    def __init__(self):
        self.tokens = ["Fixture ", "reply."]

    async def stream_chat(self, messages, **kwargs):
        for token in self.tokens:
            if isinstance(token, Exception):
                raise token
            yield token


def _replay_timer_batches(batches):
    """Exercise the actual UI handler with fake EventSource and TTS; no browser/network."""
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("Node.js is required for frontend event replay checks")
    script = r"""
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
function method(start, end) {
  const first = input.source.indexOf(start);
  const last = input.source.indexOf(end, first);
  if (first < 0 || last < 0) throw new Error('UI method boundary not found');
  return input.source.slice(first, last);
}
const methods = vm.runInNewContext('({' +
  method('    subscribeEvents(sessionId)', '    async sendMessage(') +
  method('    _applyMessageDeleted(', '    async deleteMessage(') + '})', {
  CLIENT_ID: 'fixture-client', getBaseUrl: () => '', API_PATHS: {events: id => id},
  EventSource: class { close() {} }, uiLog: () => {},
});
const tts = [];
const state = {...methods, messages: [], chatHistory: [], currentGroupId: null,
  currentSessionId: 'fixture-profile', sessions: [{id: 'fixture-profile', display_name: 'Fixture'}],
  _aseStreaming: false, _aseStreamBuf: '', ttsEnabled: true,
  _now: () => 0, $nextTick: () => {}, sendToTTS: text => tts.push(text),
};
state.subscribeEvents('fixture-profile');
const snapshot = () => JSON.parse(JSON.stringify({
  messages: state.messages, streaming: state._aseStreaming, buffer: state._aseStreamBuf, tts,
}));
const snapshots = input.batches.map(batch => {
  batch.forEach(event => state._sseSource.onmessage({data: JSON.stringify(event)}));
  return snapshot();
});
state._sseSource.onmessage({data: JSON.stringify({type: 'message_deleted', message_id: 101})});
process.stdout.write(JSON.stringify({snapshots, afterDelete: snapshot()}));
"""
    source = (Path(__file__).resolve().parents[1] / "static/js/app.js").read_text(encoding="utf-8")
    result = subprocess.run(
        [node, "-e", script], input=json.dumps({"source": source, "batches": batches}),
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=10,
    )
    return json.loads(result.stdout)


class MessageBroadcastIdTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._patches = ExitStack()
        self.addCleanup(self._patches.close)
        self.store = _FakeStore()
        self.provider = _FakeProvider()
        self.events = []
        self.session = SimpleNamespace(
            id="fixture-profile", profile_id="fixture-profile", display_name="Fixture",
            conversation_store=self.store,
        )
        self.manager = SimpleNamespace(
            get_by_id=lambda _: self.session, get_current=lambda: self.session,
        )
        self.app = SimpleNamespace(state=SimpleNamespace(
            session_manager=self.manager,
            config=SimpleNamespace(get_active_llm_preset=lambda: {
                "base_url": "https://example.invalid/v1", "model": "fixture-model",
            }),
        ))

        async def push(channel, data):
            self.assertEqual(self.session.id, channel)
            self.events.append(data)

        self._patches.enter_context(mock.patch("src.core.broadcast.broadcast.push", side_effect=push))
        for target in ("src.prompt.pipeline.build_messages", "src.api.chat.build_messages"):
            self._patches.enter_context(mock.patch(target, side_effect=lambda *a, **k: [
                {"role": "system", "content": "Fixture context."},
                {"role": "user", "content": ""},
            ]))
        for target in ("src.llm.registry.get_provider", "src.api.chat.get_provider"):
            self._patches.enter_context(mock.patch(target, return_value=self.provider))
        for target in ("src.config.effective_config.get_effective_max_history_turns",
                       "src.api.chat.get_effective_max_history_turns"):
            self._patches.enter_context(mock.patch(target, return_value=4))
        self._patches.enter_context(mock.patch("src.api.memory._get_or_create_manager"))
        self._patches.enter_context(mock.patch.multiple(
            "src.api.chat", log_llm_call=mock.DEFAULT, log_llm_response=mock.DEFAULT,
            log_error=mock.DEFAULT, log_debug=mock.DEFAULT,
        ))
        self._patches.enter_context(mock.patch.multiple(
            "src.utils.debug_logger", log_error=mock.DEFAULT, log_debug=mock.DEFAULT,
        ))

    async def _expire(self):
        await _on_expire(self.app, {
            "id": "fixture-timer", "label": "Fixture timer", "session_id": self.session.id,
        })

    async def _trigger(self):
        response = await system_trigger_endpoint(
            self.session.id, SystemTriggerRequest(message="Fixture trigger."),
            SimpleNamespace(app=self.app),
        )
        return [json.loads(chunk.removeprefix("data: ").strip())
                async for chunk in response.body_iterator]

    async def test_timer_broadcast_ids_match_each_persisted_reply(self):
        await self._expire()
        await self._expire()
        messages = [e for e in self.events if e.get("type") == "new_message"]
        self.assertEqual(2, len(self.store.rows))
        self.assertEqual([101, 102], [row["id"] for row in self.store.rows])
        self.assertEqual([row["id"] for row in self.store.rows],
                         [e.get("message_id") for e in messages])
        self.assertEqual([row["content"] for row in self.store.rows],
                         [e["content"] for e in messages])

    async def test_timer_identifies_streamed_bubble_before_token_done(self):
        await self._expire()
        messages = [e for e in self.events if e.get("type") == "new_message"]
        self.assertEqual(1, len(messages))
        self.assertEqual("", messages[0].get("client_id"))
        self.assertEqual(self.session.display_name, messages[0].get("sender"))
        self.assertEqual("Fixture reply.", "".join(
            e["token"] for e in self.events if "token" in e and not e.get("done")
        ))
        self.assertIn("token", self.events[-1])
        self.assertTrue(self.events[-1].get("done"))
        self.assertEqual(self.store.rows[0]["id"], self.events[-1].get("message_id"))
        self.assertLess(self.events.index(messages[0]), len(self.events) - 1)

    async def test_timer_empty_reply_does_not_invent_a_persisted_id(self):
        self.provider.tokens = []
        await self._expire()
        self.assertEqual([], self.store.rows)
        self.assertEqual([], self.events)

    async def _assert_failed_timer_then_success(self, tokens):
        self.provider.tokens = tokens
        await self._expire()
        failed_events = list(self.events)
        failed_rows = list(self.store.rows)
        self.provider.tokens = ["Next ", "reminder."]
        await self._expire()
        successful_events = self.events[len(failed_events):]
        ui = _replay_timer_batches([failed_events, successful_events])

        self.assertEqual([], failed_rows)
        self.assertEqual([], failed_events)
        self.assertEqual({"messages": [], "streaming": False, "buffer": "", "tts": []},
                         ui["snapshots"][0])
        self.assertEqual([101], [row["id"] for row in self.store.rows])
        self.assertEqual(["Next reminder."], [row["content"] for row in self.store.rows])
        success = ui["snapshots"][1]
        self.assertFalse(success["streaming"])
        self.assertEqual("", success["buffer"])
        self.assertEqual([101], [message["id"] for message in success["messages"]])
        self.assertEqual(["Next reminder."], [message["content"] for message in success["messages"]])
        self.assertEqual(["Next reminder."], success["tts"])
        self.assertEqual([], ui["afterDelete"]["messages"])

    async def test_timer_whitespace_then_success_has_no_failed_bubble_or_tts(self):
        await self._assert_failed_timer_then_success([" ", "\n"])

    async def test_timer_error_token_then_success_has_no_failed_bubble_or_tts(self):
        await self._assert_failed_timer_then_success(["[LLM Error: fixture timeout]"])

    async def test_timer_partial_exception_then_success_has_no_failed_bubble_or_tts(self):
        await self._assert_failed_timer_then_success(["Partial fixture", RuntimeError("Fixture failure")])

    async def test_timer_partial_error_token_then_success_has_no_failed_bubble_or_tts(self):
        await self._assert_failed_timer_then_success(["Partial fixture", "[LLM Error: fixture timeout]"])

    async def test_timer_store_failure_does_not_start_a_ui_stream(self):
        with mock.patch.object(self.store, "append", side_effect=RuntimeError("Fixture store failure")):
            await self._expire()
        self.assertEqual([], self.events)
        self.assertEqual([], self.store.rows)

    async def test_timer_successes_publish_only_after_persisting_and_keep_ui_tts_separate(self):
        original_append = self.store.append

        def append_after_generation(*args, **kwargs):
            # A prior completed reminder has exactly three events; the current one has none yet.
            self.assertEqual(3 * len(self.store.rows), len(self.events))
            return original_append(*args, **kwargs)

        batches = []
        with mock.patch.object(self.store, "append", side_effect=append_after_generation):
            for tokens in (["**First", " reminder.**"], ["Second", " reminder."]):
                before = len(self.events)
                self.provider.tokens = tokens
                await self._expire()
                batches.append(self.events[before:])

        self.assertEqual(2, len(self.store.rows))
        for batch, row in zip(batches, self.store.rows):
            self.assertEqual(3, len(batch))
            self.assertEqual({"type": "token", "token": row["content"], "done": False}, batch[0])
            self.assertEqual("new_message", batch[1]["type"])
            self.assertEqual(row["id"], batch[1]["message_id"])
            self.assertTrue(batch[2]["done"])
            self.assertEqual(row["id"], batch[2]["message_id"])
        ui = _replay_timer_batches(batches)
        success = ui["snapshots"][-1]
        self.assertFalse(success["streaming"])
        self.assertEqual("", success["buffer"])
        self.assertEqual([101, 102], [message["id"] for message in success["messages"]])
        self.assertEqual(["First reminder.", "Second reminder."],
                         [message["content"] for message in success["messages"]])
        self.assertEqual(["First reminder.", "Second reminder."], success["tts"])
        self.assertEqual([102], [message["id"] for message in ui["afterDelete"]["messages"]])

    async def test_system_trigger_success_shares_id_between_broadcast_and_done(self):
        frames = await self._trigger()
        self.assertEqual(1, len(self.store.rows))
        self.assertEqual(1, len(self.events))
        self.assertEqual(self.store.rows[0]["id"], self.events[0].get("message_id"))
        self.assertEqual(self.store.rows[0]["id"], frames[-1].get("message_id"))
        self.assertEqual("assistant", frames[-1].get("message_role"))
        self.assertTrue(frames[-1]["done"])

    async def test_system_trigger_saved_failures_also_share_id(self):
        for tokens in ([], ["[LLM Error: fixture failure]"]):
            with self.subTest(tokens=tokens):
                self.provider.tokens = tokens
                frames = await self._trigger()
                row = self.store.rows[-1]
                self.assertEqual("", row["content"])
                self.assertEqual(row["id"], self.events[-1].get("message_id"))
                self.assertEqual(row["id"], frames[-1].get("message_id"))
                self.assertTrue(frames[-1]["done"])
                self.assertIn("error", frames[-1])

    async def test_system_trigger_without_session_has_no_persisted_id(self):
        self.manager.get_by_id = lambda _: None
        self.manager.get_current = lambda: None
        frames = await self._trigger()
        self.assertEqual([], self.store.rows)
        self.assertEqual([], self.events)
        self.assertTrue(frames[-1]["done"])
        self.assertNotIn("message_id", frames[-1])


if __name__ == "__main__":
    unittest.main()
