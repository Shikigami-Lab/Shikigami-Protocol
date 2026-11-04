"""Exercise the single-chat endpoint with a real task store and fake models."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.agent.runtime import AgentRuntime
from src.api import chat
from src.memory.conversation_store import ConversationStore


CHAT = {"base_url": "https://chat.fixture/v1", "model": "chat-model"}
TOOL = {"base_url": "https://tool.fixture/v1", "model": "tool-model"}
HANDOFF = '<shikigami-task>{"goal":"Write a file"}</shikigami-task>'


class ChatTaskModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.history = ConversationStore(self.temp.name)
        self.addCleanup(self.history.close)
        self.session = SimpleNamespace(id="fixture", profile_id="fixture", display_name="Fixture",
                                       conversation_store=self.history, storage_root=self.temp.name, save_runtime_state=Mock())
        self.app = SimpleNamespace(state=SimpleNamespace(
            config=SimpleNamespace(agent={"enabled": True, "model_preset": "cheap"},
                                   llm_presets={"cheap": TOOL}, get_active_llm_preset=lambda: CHAT),
            session_manager=SimpleNamespace(get_by_id=lambda _: self.session)))
        self.runtime = AgentRuntime(self.app, root=self.temp.name, profile_loader=lambda _: {})
        self.addAsyncCleanup(self.runtime.close)
        self.app.state.agent_runtime = self.runtime
        self.request = SimpleNamespace(app=self.app)
        self.calls = []
        self.worker_state = "completed"
        self.chat_replies = [["A normal ", "chat reply"]]
        guards = self.enterContext(ExitStack())
        for name in ("_fire_memory_tasks", "_fire_portrait_burst_check", "_fire_persona_evolution_check",
                     "log_chat_request", "log_llm_call", "log_llm_response"):
            guards.enter_context(patch.object(chat, name))
        guards.enter_context(patch("src.api.agent._same_origin"))
        guards.enter_context(patch("src.api.memory._get_or_create_manager"))
        guards.enter_context(patch("src.core.broadcast.broadcast.push", AsyncMock()))
        guards.enter_context(patch.object(chat, "get_effective_max_history_turns", return_value=10))
        guards.enter_context(patch.object(chat, "build_messages", side_effect=lambda *a, **k: [
            {"role": "system", "content": "Character context"}, {"role": "user", "content": a[1]}]))
        guards.enter_context(patch.object(chat, "get_provider", side_effect=self.provider))
        guards.enter_context(patch.object(self.runtime, "_drive", side_effect=self.drive))

    def provider(self, preset):
        async def stream(messages):
            self.calls.append((preset["model"], json.dumps(messages, ensure_ascii=False)))
            for chunk in self.chat_replies.pop(0):
                yield chunk
        return SimpleNamespace(stream_chat=stream)

    async def drive(self, run):
        for step in range(3):
            self.calls.append((run.gateway.route["model"], step))
            await self.runtime._publish(run, {"type": "tool_call", "tool_name": "fixture",
                                               "call_id": str(step), "arguments": "{}"})
        await self.runtime._publish(run, {"type": "tool_result", "tool_name": "fixture",
            "call_id": "2", "content": [{"type": "text", "text": "Verified file receipt"}]})
        await self.runtime._publish(run, {"type": "reply_delta", "text": "PRIVATE_TOOL_NARRATION"})
        await self.runtime._publish(run, {"type": self.worker_state})
        await run.queue.put(None)

    async def send(self, **kwargs):
        response = await chat.chat_endpoint(self.request, chat.ChatRequest(
            message="Fixture input", session_id="fixture", **kwargs))
        return [json.loads(frame.removeprefix("data: ")) async for frame in response.body_iterator]

    async def test_ordinary_chat_uses_chat_model_once_with_tools_enabled(self):
        frames = await self.send()
        self.assertEqual([model for model, _ in self.calls], ["chat-model"])
        self.assertEqual("".join(frame.get("token", "") for frame in frames), "A normal chat reply")
        self.assertEqual(self.history.get_recent()[-1]["content"], "A normal chat reply")

    async def test_task_uses_tool_model_only_between_chat_calls(self):
        self.chat_replies = [[HANDOFF[:6], HANDOFF[6:]], ["Here is the result."]]
        frames = await self.send()
        self.assertEqual([model for model, _ in self.calls],
                         ["chat-model", "tool-model", "tool-model", "tool-model", "chat-model"])
        self.assertIn("Verified file receipt", self.calls[-1][1])
        self.assertEqual("".join(frame.get("token", "") for frame in frames), "Here is the result.")
        self.assertNotIn("PRIVATE_TOOL_NARRATION", json.dumps(frames))
        history = self.history.get_recent()
        self.assertEqual(history[-1]["content"], "Here is the result.")
        self.assertIsNotNone(history[-1]["agent_run_id"])
        self.assertEqual(history[0]["agent_run_id"], history[-1]["agent_run_id"])

    async def test_failed_task_explanation_is_written_by_chat_model(self):
        self.worker_state = "failed"
        self.chat_replies = [[HANDOFF], ["I could only finish part of the work."]]
        frames = await self.send()
        self.assertEqual([model for model, _ in self.calls],
                         ["chat-model", "tool-model", "tool-model", "tool-model", "chat-model"])
        self.assertIn('"state": "failed"', json.loads(self.calls[-1][1])[0]["content"])
        self.assertEqual("".join(frame.get("token", "") for frame in frames),
                         "I could only finish part of the work.")
        self.assertNotIn("PRIVATE_TOOL_NARRATION", json.dumps(frames))
        self.assertEqual(self.history.get_recent()[-1]["generation_status"], "failed")

    async def test_continue_uses_tool_model_before_chat_reply(self):
        self.chat_replies = [[HANDOFF], ["First reply."]]
        first = await self.send()
        prior = first[-1]["agent_run_id"]
        self.calls.clear()
        self.chat_replies = [["Continued."]]
        frames = await self.send(continue_run_id=prior)
        self.assertEqual([model for model, _ in self.calls],
                         ["tool-model", "tool-model", "tool-model", "chat-model"])
        self.assertEqual("".join(frame.get("token", "") for frame in frames), "Continued.")
        self.assertEqual(self.runtime.store.get_run(frames[-1]["agent_run_id"])["parent_run_id"], prior)

    async def test_unavailable_tool_preset_does_not_block_clarification(self):
        self.app.state.config.llm_presets.clear()
        self.chat_replies = [["Which file should I use?"]]
        frames = await self.send()
        self.assertEqual([model for model, _ in self.calls], ["chat-model"])
        self.assertEqual("".join(frame.get("token", "") for frame in frames), "Which file should I use?")

    async def test_malformed_handoff_does_not_start_task_or_reach_chat(self):
        self.chat_replies = [['<shikigami-task>{"goal":"Incomplete"']]
        frames = await self.send()
        self.assertEqual([model for model, _ in self.calls], ["chat-model"])
        self.assertNotIn("shikigami-task", json.dumps(frames))
        self.assertEqual([row["role"] for row in self.history.get_recent()], ["user"])

    async def test_duplicate_task_request_does_not_write_or_bill_again(self):
        self.chat_replies = [[HANDOFF], ["Finished."]]
        await self.send(request_id="same-request")
        calls = self.calls.copy()
        history = self.history.get_recent()
        duplicate = await self.send(request_id="same-request")
        self.assertIn("already exists", duplicate[-1]["error"])
        self.assertEqual(self.calls, calls)
        self.assertEqual(self.history.get_recent(), history)

    async def test_stop_during_worker_finish_prevents_chat_synthesis(self):
        self.chat_replies = [[HANDOFF]]
        real_publish = self.runtime._publish

        async def publish(run, event):
            if event["type"] == "completed":
                run.cancel_requested = True
            await real_publish(run, event)

        with patch.object(self.runtime, "_publish", side_effect=publish):
            frames = await self.send()
        self.assertTrue(frames[-1]["cancelled"])
        self.assertEqual([model for model, _ in self.calls],
                         ["chat-model", "tool-model", "tool-model", "tool-model"])
        self.assertEqual([row["role"] for row in self.history.get_recent()], ["user"])

    async def test_fenced_handoff_never_exposes_the_command(self):
        self.chat_replies = [[chr(96) * 3, '\n<shik', 'igami-task>{"goal":"SECRET"}</shikigami-task>']]
        frames = await self.send()
        self.assertEqual([model for model, _ in self.calls], ["chat-model"])
        self.assertNotIn("SECRET", json.dumps(frames))
        self.assertNotIn("shikigami-task", json.dumps(frames))
        self.assertEqual([row["role"] for row in self.history.get_recent()], ["user"])

    async def test_provider_error_after_prefix_is_not_written_as_speech(self):
        self.chat_replies = [["Visible prefix", "[LLM Error: private provider detail]"]]
        frames = await self.send()
        self.assertIn("error", frames[-1])
        self.assertNotIn("private provider detail", json.dumps(frames))
        self.assertEqual([row["role"] for row in self.history.get_recent()], ["user"])


class ChatProviderCompatibilityTests(unittest.TestCase):
    def test_native_google_base_uses_chat_completions_compatibility_endpoint(self):
        from src.llm.openai_compat import _normalize_base_url

        self.assertEqual(_normalize_base_url("https://generativelanguage.googleapis.com/v1beta/"),
                         "https://generativelanguage.googleapis.com/v1beta/openai")
        self.assertEqual(_normalize_base_url("https://openrouter.ai/api/v1/chat/completions"),
                         "https://openrouter.ai/api/v1")
