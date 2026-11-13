"""History invalidation races: real runtime/SQLite, synthetic worker, no model calls.

Run with Python -B. Chat tests execute the actual endpoint with synthetic models.
Prompt/config loading and background effects are replaced; no real data is read.
"""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.agent.runtime import AgentRuntime
from src.api.sessions import clear_session_history
from src.core.broadcast import broadcast
from src.memory.conversation_store import ConversationStore


PRESET = {"base_url": "https://fixture.invalid/v1", "model": "fixture",
          "api_key": "synthetic-history-race-key"}
MESSAGES = [{"role": "system", "content": "Synthetic context"},
            {"role": "user", "content": "Synthetic request"}]


class AgentHistoryRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shikigami-history-race-")
        self.addCleanup(self.tmp.cleanup)
        for target, replacement in (
            ("src.agent.runtime.asyncio.create_subprocess_exec",
             AsyncMock(side_effect=AssertionError("No worker processes"))),
            ("src.agent.runtime.subprocess.run",
             Mock(side_effect=AssertionError("No subprocesses"))),
            ("src.agent.model_gateway.ModelGateway.relay",
             AsyncMock(side_effect=AssertionError("No model requests"))),
            ("src.core.broadcast.broadcast.push", AsyncMock()),
        ):
            guard = patch(target, replacement)
            guard.start()
            self.addCleanup(guard.stop)
        self.store = ConversationStore(str(Path(self.tmp.name) / "conversation"))
        self.addCleanup(self.store.close)
        self.session = SimpleNamespace(id="alpha", profile_id="alpha", display_name="Fixture",
                                       conversation_store=self.store)
        config = SimpleNamespace(agent={"enabled": True}, get_active_llm_preset=lambda: dict(PRESET))
        self.app = SimpleNamespace(state=SimpleNamespace(
            config=config, session_manager=SimpleNamespace(get_by_id=lambda _: self.session)))
        timers = Mock(spec=["start_timer", "list_timers", "stop_timer"])
        self.runtime = AgentRuntime(self.app, root=self.tmp.name, timer_manager=timers,
                                    profile_loader=lambda _: {"tool_configs": {"timer": {"enabled": True}}})
        self.app.state.agent_runtime = self.runtime
        self.request = SimpleNamespace(app=self.app, headers={})

    async def asyncTearDown(self):
        await self.runtime.close()

    def prepare(self):
        return self.runtime.prepare(self.session, deepcopy(MESSAGES), dict(PRESET))

    async def cancelled_reply(self):
        from src.api import chat

        async def first_reply(messages):
            yield '<shikigami-task>{"goal":"Synthetic task"}</shikigami-task>'

        async def drive(item):
            await self.runtime._publish(item, {"type": "started"})
            await self.runtime._publish(item, {"type": "reply_delta", "text": "private task text"})
            await self.runtime._publish(item, {"type": "cancelled"})
            await item.queue.put(None)

        self.session.save_runtime_state = Mock()
        for guard in (
            patch.object(self.runtime, "_drive", side_effect=drive),
            patch("src.api.agent._same_origin"),
            patch("src.api.memory.get_or_create_manager"),
            patch.object(chat, "get_effective_max_history_turns", return_value=10),
            patch.object(chat, "build_messages", return_value=deepcopy(MESSAGES)),
            patch.object(chat, "get_provider", return_value=SimpleNamespace(stream_chat=first_reply)),
            patch.object(chat, "log_chat_request"),
            patch.object(chat, "log_llm_call"),
        ):
            self.enterContext(guard)
        response = await chat.chat_endpoint(self.request, chat.ChatRequest(
            message="Synthetic task request", session_id="alpha", request_id="fixture"))
        stream = response.body_iterator
        user = json.loads((await anext(stream)).removeprefix("data: "))
        self.assertEqual(user["message_role"], "user")
        started = json.loads((await anext(stream)).removeprefix("data: "))["agent_event"]
        run = self.runtime._runs[started["run_id"]]
        await run.task
        self.assertEqual(self.runtime.store.get_run(run.id)["state"], "cancelled")
        return run, stream

    async def finish_reply(self, stream):
        frames = [json.loads(frame.removeprefix("data: ").strip()) async for frame in stream]
        done = [frame for frame in frames if frame.get("done")]
        self.assertEqual(len(done), 1)
        return done[0]

    def pause_invalidation_workers(self, count):
        """Hold each real invalidation before it can acquire its lock/update SQL."""
        loop = asyncio.get_running_loop()
        entered = [asyncio.Event() for _ in range(count)]
        releases = [threading.Event() for _ in range(count)]
        original = self.runtime._invalidate_state
        assignment = threading.Lock()
        claimed = 0

        def paused(profile_id):
            nonlocal claimed
            with assignment:
                index = claimed
                claimed += 1
            loop.call_soon_threadsafe(entered[index].set)
            if not releases[index].wait(10):
                raise AssertionError("Test did not release invalidation worker")
            return original(profile_id)

        return entered, releases, patch.object(self.runtime, "_invalidate_state", side_effect=paused)

    async def test_clear_during_pending_invalidation_discards_partial_and_blocks_prepare(self):
        self.store.append("user", "history to delete")
        run, stream = await self.cancelled_reply()
        entered, releases, guard = self.pause_invalidation_workers(1)
        with guard:
            deletion = asyncio.create_task(clear_session_history("alpha", self.request, type=None))
            try:
                await asyncio.wait_for(entered[0].wait(), 3)
                self.assertFalse(deletion.done())
                self.assertEqual(self.store.get_recent(), [])
                # The SQL revision has NOT changed: this exercises the pre-await
                # barrier, not the already-existing persisted stale check.
                self.assertTrue(self.runtime.store.is_current(run.id, "alpha"))
                with patch.object(self.store, "append", wraps=self.store.append) as append:
                    done = await self.finish_reply(stream)
                    self.assertTrue(done["discard_partial"])
                    self.assertTrue(done["cancelled"])
                    self.assertIsNone(done.get("message_id"))
                    append.assert_not_called()
                self.assertFalse(self.runtime.context_valid(run))
                with self.assertRaisesRegex(ValueError, "history is being changed"):
                    self.prepare()
                releases[0].set()
                self.assertEqual(await asyncio.wait_for(deletion, 3), {"ok": True, "compacted": False})
                self.assertEqual(self.store.get_recent(), [])
                self.assertFalse(self.runtime.store.is_current(run.id, "alpha"))
                fresh = self.prepare()
                self.assertTrue(self.runtime.context_valid(fresh))
                self.assertGreater(fresh.revision, run.revision)
            finally:
                releases[0].set()
                await asyncio.gather(deletion, return_exceptions=True)
                await stream.aclose()

    async def test_cancel_without_history_mutation_keeps_only_user_message(self):
        run, stream = await self.cancelled_reply()
        try:
            done = await self.finish_reply(stream)
            self.assertTrue(done["cancelled"])
            self.assertFalse(done["discard_partial"])
            self.assertIsNone(done.get("message_id"))
            history = self.store.get_recent()
            self.assertEqual([(row["role"], row["content"]) for row in history],
                             [("user", "Synthetic task request")])
            self.assertEqual(history[0]["agent_run_id"], run.id)
            self.assertIsNone(self.runtime.store.get_run(run.id)["assistant_message_id"])
            self.assertTrue(self.runtime.context_valid(run))
        finally:
            await stream.aclose()

    async def _plain_reply_cleared_during_stream(self, *, eof_after_clear):
        from src.api import chat
        import src.utils.debug_logger as debug

        release = asyncio.Event()

        async def plain_stream(messages):
            yield "old-context partial"
            await release.wait()
            if not eof_after_clear:
                yield " stale suffix"

        self.session.save_runtime_state = Mock()
        body = chat.ChatRequest(message="Synthetic foreground request", session_id="alpha",
                                client_id="fixture", request_id="plain-fixture", conversation_only=True)
        with ExitStack() as guards:
            # Exercise production revision capture, token guards and final-save
            # guard. Patch only external/config dependencies and side effects.
            for module in (chat, debug):
                for name in vars(module):
                    if name.startswith("log_") and callable(getattr(module, name)):
                        guards.enter_context(patch.object(module, name))
            guards.enter_context(patch("src.api.agent._same_origin"))
            guards.enter_context(patch("src.api.memory.get_or_create_manager"))
            guards.enter_context(patch.object(chat, "get_effective_max_history_turns", return_value=10))
            guards.enter_context(patch.object(chat, "build_messages", return_value=deepcopy(MESSAGES)))
            provider = guards.enter_context(patch.object(chat, "get_provider",
                return_value=SimpleNamespace(stream_chat=plain_stream)))
            memory_tasks = guards.enter_context(patch.object(chat, "_fire_memory_tasks"))
            guards.enter_context(patch.object(chat, "_fire_portrait_burst_check"))
            guards.enter_context(patch.object(chat, "_fire_persona_evolution_check"))
            prepare = guards.enter_context(patch.object(self.runtime, "prepare",
                side_effect=AssertionError("conversation_only must use the plain provider")))
            append = guards.enter_context(patch.object(self.store, "append", wraps=self.store.append))
            revision = self.runtime.store.revision("alpha")
            response = await chat.chat_endpoint(self.request, body)
            stream = response.body_iterator
            try:
                first = json.loads((await anext(stream)).removeprefix("data: ").strip())
                self.assertEqual(first["message_role"], "user")
                second = json.loads((await anext(stream)).removeprefix("data: ").strip())
                self.assertEqual(second["token"], "old-context partial")
                provider.assert_called_once()
                self.assertEqual(await clear_session_history("alpha", self.request, type=None), {"ok": True, "compacted": False})
                self.assertGreater(self.runtime.store.revision("alpha"), revision)
                release.set()
                frames = [json.loads(frame.removeprefix("data: ").strip()) async for frame in stream]
                done = [frame for frame in frames if frame.get("done")]
                self.assertEqual(len(done), 1)
                self.assertTrue(done[0].get("cancelled"))
                self.assertTrue(done[0].get("discard_partial"))
                self.assertFalse(done[0].get("error"))
                self.assertIsNone(done[0].get("message_id"))
                self.assertFalse(any(frame.get("token") for frame in frames))
                self.assertEqual(self.store.get_all_with_id(), [])
                self.assertFalse(any(call.args[0] == "assistant" for call in append.call_args_list))
                self.assertFalse(any(call.args[1].get("role") == "assistant"
                                     for call in broadcast.push.await_args_list))
                memory_tasks.assert_not_called()
                prepare.assert_not_called()
                self.assertNotIn("alpha", self.app.state.chat_streaming_sessions)
                self.assertEqual(self.app.state.chat_stream_counts["alpha"], 0)
            finally:
                release.set()
                await stream.aclose()

    async def test_foreground_conversation_only_clear_midstream_discards_next_token(self):
        await self._plain_reply_cleared_during_stream(eof_after_clear=False)

    async def test_foreground_conversation_only_clear_before_eof_blocks_final_save(self):
        await self._plain_reply_cleared_during_stream(eof_after_clear=True)

    async def test_overlapping_invalidations_keep_prepare_blocked_until_both_finish(self):
        entered, releases, guard = self.pause_invalidation_workers(2)
        tasks = []
        with guard:
            try:
                for index in range(2):
                    tasks.append(asyncio.create_task(
                        clear_session_history("alpha", self.request, type=None)))
                    await asyncio.wait_for(entered[index].wait(), 3)
                releases[0].set()
                self.assertEqual(await asyncio.wait_for(tasks[0], 3), {"ok": True, "compacted": False})
                self.assertFalse(tasks[1].done())
                with self.assertRaisesRegex(ValueError, "history is being changed"):
                    self.prepare()
                releases[1].set()
                self.assertEqual(await asyncio.wait_for(tasks[1], 3), {"ok": True, "compacted": False})
                self.assertTrue(self.runtime.context_valid(self.prepare()))
            finally:
                for release in releases:
                    release.set()
                await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
