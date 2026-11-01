"""Offline coverage for timer receipts entering existing reflection cycles."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.core.reflection import ReflectionEngine
from src.core.session import Session
from src.tools.timer.expire_handler import _on_expire


class ReflectionResultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.session = Session("session-a", "profile-a", "Fixture", str(self.root))
        self.session._store = mock.Mock()
        self.session._store.get_recent.return_value = [{"role": "user", "content": "hello"}]
        self.rows = [{"id": "result-1", "payload": {"type": "timer_expired", "label": "tea"}}]
        self.saved_at_ack = []

        def ack(ids):
            self.saved_at_ack.append(json.loads(self.state_path.read_text(encoding="utf-8")))
            self.rows[:] = [r for r in self.rows if r["id"] not in ids]

        self.runtime = SimpleNamespace(
            reflection_results=mock.Mock(side_effect=lambda profile_id: copy.deepcopy(self.rows)),
            acknowledge_reflection_results=mock.Mock(side_effect=ack),
            record_timer_expiry=mock.Mock(),
        )
        config = SimpleNamespace(
            secondary_models={}, user_name="User", user_persona={},
            get_reflection_config=lambda: {}, get_ase_config=lambda: {},
            get_active_llm_preset=lambda: {"base_url": "https://example.invalid"},
        )
        self.app = SimpleNamespace(state=SimpleNamespace(config=config, agent_runtime=self.runtime))
        self.engine = ReflectionEngine(self.app)
        self.provider = SimpleNamespace(chat=mock.AsyncMock(return_value=json.dumps({
            "thought": "The tea timer finished.", "style_hint": "calm", "urgency": 0.2,
            "next_reflection_in": 123,
        })))
        self.patch("src.core.reflection.get_project_root", return_value=str(self.root))
        self.patch("src.core.reflection.get_locale", return_value="en")
        self.patch("src.core.reflection.build_reflection_messages", side_effect=lambda *a: [
            {"role": "system", "content": "Fixture reflection."},
            {"role": "user", "content": "Reflect now."},
        ])
        self.patch("src.core.ase.get_ase_context_for_reflection", return_value=None)
        self.patch("src.config.effective_config.get_effective_topic_discovery_config", return_value={})
        self.patch("src.prompt.segments.time_context.get_time_context_notes", return_value=[])
        self.patch("src.llm.registry.get_provider", return_value=self.provider)
        for name in ("log_reflection_result", "log_reflection_skip", "log_secondary_llm_call",
                     "log_secondary_llm_response", "log_debug", "log_error"):
            self.patch("src.utils.debug_logger." + name)
        self.stack.enter_context(mock.patch.object(self.engine, "_get_preset", return_value=({"model": "fake"}, False, "fake")))
        self.stack.enter_context(mock.patch.object(self.engine, "_get_fallback_preset", return_value=None))
        self.stack.enter_context(mock.patch.object(self.engine, "_load_emotion", return_value=None))
        self.stack.enter_context(mock.patch.object(self.engine, "_load_affinity", return_value=None))

    @property
    def state_path(self):
        return self.root / "runtime_state.json"

    def patch(self, target, **kwargs):
        return self.stack.enter_context(mock.patch(target, **kwargs))

    async def reflect(self):
        await self.engine._reflect(self.session, {})

    async def test_pending_character_plans_are_reflection_data_not_user_todos(self):
        from src.tools.todo.store import TodoStore
        store = TodoStore(self.session.storage_root)
        store.add('Synthetic user responsibility')
        store.add('Synthetic unfinished fox drawing', owner='ai')
        self.runtime.access_for = mock.Mock(return_value={'todo_enabled': True})
        self.runtime.settings = {'enabled': True}
        await self.reflect()
        system = self.provider.chat.call_args.args[0][0]['content']
        self.assertIn('Synthetic unfinished fox drawing', system)
        self.assertNotIn('Synthetic user responsibility', system)

    async def test_pending_plan_can_be_considered_without_recent_chat(self):
        from src.tools.todo.store import TodoStore
        self.session._store.get_recent.return_value = []
        self.rows[:] = []
        TodoStore(self.session.storage_root).add('Synthetic ongoing project', owner='ai')
        self.runtime.access_for = mock.Mock(return_value={'todo_enabled': True})
        self.runtime.settings = {'enabled': True}
        await self.reflect()
        self.provider.chat.assert_awaited_once()
        self.assertIn('Synthetic ongoing project', self.provider.chat.call_args.args[0][0]['content'])

    async def test_saved_reflection_starts_real_runtime_and_result_does_not_loop(self):
        from src.agent.runtime import AgentRuntime
        self.session.id = self.session.profile_id
        self.app.state.session_manager = SimpleNamespace(get_current=lambda: self.session)
        self.app.state.config.agent = {'enabled': True, 'workspace_enabled': True,
            'autonomy_enabled': True, 'autonomy_interval_seconds': 60}
        self.app.state.config.get_active_llm_preset = lambda: {'base_url': 'http://localhost:9999/v1', 'model': 'fake'}
        actual = AgentRuntime(self.app, root=self.root, profile_loader=lambda p: {
            'tool_configs': {'agent': {'autonomy_enabled': True}}})
        self.app.state.agent_runtime = actual
        self.provider.chat.return_value = json.dumps({'thought': 'Make a note', 'action': 'write a note'})
        started = []

        async def drive(run):
            started.append(run.profile_id)
            (actual.workspaces.directory(run.profile_id) / 'reflection.txt').write_text('synthetic note')
            await actual._publish(run, {'type': 'tool_call', 'tool_name': 'write', 'arguments': '{}'})
            await actual._publish(run, {'type': 'completed'})
            await run.queue.put(None)

        with mock.patch.object(actual.autonomy, '_build_messages', return_value=[
                {'role': 'system', 'content': 'Synthetic character'}, {'role': 'user', 'content': 'Make a note'}]), \
                mock.patch.object(actual, '_drive', side_effect=drive):
            try:
                await self.reflect()
                import asyncio
                await asyncio.gather(*list(actual.autonomy.tasks))
                self.assertEqual(started, [self.session.profile_id])
                self.assertEqual(actual.store.list_runs(self.session.profile_id)[0]['origin'], 'reflection')
                self.assertTrue(actual.reflection_results(self.session.profile_id))
                await self.reflect()
                self.assertEqual(started, [self.session.profile_id])
                self.assertEqual(actual.reflection_results(self.session.profile_id), [])
            finally:
                await actual.close()

    async def test_failed_reflection_save_cannot_launch_action(self):
        autonomy = SimpleNamespace(enabled=lambda session: True, start=mock.AsyncMock())
        self.runtime.autonomy = autonomy
        self.provider.chat.return_value = json.dumps({'thought': 'Make a note', 'action': 'write a note'})
        with mock.patch.object(self.session, 'save_runtime_state', return_value=None):
            await self.reflect()
        autonomy.start.assert_not_awaited()

    async def test_results_are_system_facts_and_ack_follows_persistence(self):
        await self.reflect()
        self.runtime.reflection_results.assert_called_once_with("profile-a")
        messages = self.provider.chat.call_args.args[0]
        self.assertEqual(["system", "user"], [m["role"] for m in messages])
        self.assertIn("Factual tool results", messages[0]["content"])
        self.assertIn("timer_expired", messages[0]["content"])
        self.assertEqual("Reflect now.", messages[1]["content"])
        self.runtime.acknowledge_reflection_results.assert_called_once_with(["result-1"])
        self.assertEqual(["result-1"], self.saved_at_ack[0]["reflection_state"]["consumed_result_ids"])
        self.assertEqual(123, self.session.reflection_state["next_reflection_in"])
        self.assertEqual(1, self.provider.chat.await_count)

    async def test_batch_limit_and_later_cycle(self):
        self.rows[:] = [{"id": f"result-{i}", "payload": {"n": i}} for i in range(25)]
        await self.reflect()
        self.runtime.acknowledge_reflection_results.assert_called_once()
        self.assertEqual(20, len(self.runtime.acknowledge_reflection_results.call_args.args[0]))
        self.assertEqual(5, len(self.rows))
        await self.reflect()
        self.assertEqual([], self.rows)
        self.assertEqual(2, self.provider.chat.await_count)

    async def test_model_error_leaves_pending(self):
        self.provider.chat.side_effect = RuntimeError("offline fixture error")
        await self.reflect()
        self.runtime.acknowledge_reflection_results.assert_not_called()
        self.assertEqual(1, len(self.rows))

    async def test_empty_and_invalid_outputs_leave_pending(self):
        for raw in ("", "not json", "{}", '{"thought":""}', '{"thought":null}', '{"thought":[]}'):
            self.provider.chat.return_value = raw
            await self.reflect()
        self.runtime.acknowledge_reflection_results.assert_not_called()
        self.assertEqual(1, len(self.rows))

    async def test_swallowed_save_error_leaves_pending_and_restores_state(self):
        previous = {"thought": "Before", "urgency": 0.1, "next_reflection_in": 42}
        self.session.reflection_state = previous
        self.session.save_runtime_state()
        with mock.patch("src.core.session.json.dump", side_effect=OSError("disk full")):
            await self.reflect()
        self.runtime.acknowledge_reflection_results.assert_not_called()
        self.assertEqual(previous, self.session.reflection_state)
        await self.reflect()
        self.assertEqual([], self.rows)

    async def test_raised_save_error_does_not_ack(self):
        with mock.patch.object(self.session, "save_runtime_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.reflect()
        self.runtime.acknowledge_reflection_results.assert_not_called()
        self.assertIsNone(self.session.reflection_state)

    async def test_stale_disk_state_does_not_ack(self):
        self.session.reflection_state = {"thought": "Before"}
        self.session.save_runtime_state()
        with mock.patch.object(self.session, "save_runtime_state", return_value=None):
            await self.reflect()
        self.runtime.acknowledge_reflection_results.assert_not_called()
        self.assertEqual({"thought": "Before"}, self.session.reflection_state)

    async def test_restart_after_save_before_ack_reconciles_without_model(self):
        self.runtime.acknowledge_reflection_results.side_effect = RuntimeError("ack unavailable")
        await self.reflect()
        self.assertTrue(self.state_path.exists())
        previous_store = self.session._store
        self.session = Session("session-a", "profile-a", "Fixture", str(self.root))
        self.session._store = previous_store
        self.session.load_runtime_state()
        self.runtime.acknowledge_reflection_results.side_effect = None
        self.runtime.acknowledge_reflection_results.reset_mock()
        self.provider.chat.reset_mock()
        await self.reflect()
        self.provider.chat.assert_not_called()
        self.runtime.acknowledge_reflection_results.assert_called_once_with(["result-1"])

    async def test_old_ids_not_reinjected_with_new_results(self):
        self.runtime.acknowledge_reflection_results.side_effect = RuntimeError("ack unavailable")
        await self.reflect()
        self.rows.append({"id": "result-2", "payload": {"type": "new-result"}})
        self.runtime.acknowledge_reflection_results.side_effect = None
        await self.reflect()
        system = self.provider.chat.call_args.args[0][0]["content"]
        self.assertNotIn("timer_expired", system)
        self.assertIn("new-result", system)

    async def test_session_switch_during_model_call_does_not_rebind(self):
        other = Session("session-b", "profile-b", "Other", str(self.root / "other"))
        self.app.state.session_manager = SimpleNamespace(get_current=lambda: self.session)
        response = self.provider.chat.return_value
        async def chat(*args, **kwargs):
            self.app.state.session_manager.get_current = lambda: other
            return response
        self.provider.chat.side_effect = chat
        await self.reflect()
        self.runtime.reflection_results.assert_called_once_with("profile-a")
        self.assertIsNone(other.reflection_state)
        self.assertEqual(["result-1"], self.saved_at_ack[0]["reflection_state"]["consumed_result_ids"])

    async def test_no_recent_turns_retains_existing_gate(self):
        self.session._store.get_recent.return_value = []
        await self.reflect()
        self.provider.chat.assert_not_called()
        self.runtime.acknowledge_reflection_results.assert_not_called()

    async def test_runtime_absent_keeps_existing_reflection(self):
        del self.app.state.agent_runtime
        await self.reflect()
        self.assertEqual(1, self.provider.chat.await_count)
        self.assertEqual("The tea timer finished.", self.session.reflection_state["thought"])

    async def test_arrival_during_model_call_waits_for_next_cycle(self):
        response = self.provider.chat.return_value
        async def chat(*args, **kwargs):
            self.rows.append({"id": "result-late", "payload": {"type": "late-expiry"}})
            return response
        self.provider.chat.side_effect = chat
        await self.reflect()
        self.assertEqual(["result-late"], [r["id"] for r in self.rows])
        self.assertNotIn("late-expiry", self.provider.chat.call_args.args[0][0]["content"])
        self.provider.chat.side_effect = None
        await self.reflect()
        self.assertIn("late-expiry", self.provider.chat.call_args.args[0][0]["content"])
        self.assertEqual([], self.rows)

    async def test_reconciliation_failure_does_not_reinject_or_lose_journal(self):
        self.runtime.acknowledge_reflection_results.side_effect = RuntimeError("ack unavailable")
        await self.reflect()
        saved = copy.deepcopy(self.session.reflection_state)
        self.rows.append({"id": "result-2", "payload": {"type": "new-result"}})
        self.provider.chat.reset_mock()
        await self.reflect()
        self.provider.chat.assert_not_called()
        self.assertEqual(saved, self.session.reflection_state)
        self.assertEqual(2, len(self.rows))

    async def test_retrieval_failure_keeps_ordinary_reflection(self):
        self.runtime.reflection_results.side_effect = RuntimeError("outbox unavailable")
        await self.reflect()
        self.assertEqual(1, self.provider.chat.await_count)
        self.runtime.acknowledge_reflection_results.assert_not_called()

    async def test_no_pending_results_keeps_ordinary_reflection(self):
        self.rows.clear()
        await self.reflect()
        self.assertEqual(1, self.provider.chat.await_count)
        self.assertNotIn("Factual tool results", self.provider.chat.call_args.args[0][0]["content"])
        self.runtime.acknowledge_reflection_results.assert_not_called()

    async def test_timer_expiry_enters_next_existing_reflection(self):
        self.rows.clear()
        self.app.state.config.get_active_llm_preset = lambda: {}
        self.app.state.session_manager = SimpleNamespace(get_by_id=lambda _: self.session)
        timer = {"id": "timer-1", "session_id": self.session.id, "label": "tea"}
        def record(info, profile_id):
            self.assertEqual(self.session.profile_id, profile_id)
            self.rows.append({"id": "expiry-1", "payload": {"type": "timer_expired", "timer": info}})
        self.runtime.record_timer_expiry.side_effect = record
        await _on_expire(self.app, timer)
        self.provider.chat.assert_not_called()
        await self.reflect()
        self.assertEqual(1, self.provider.chat.await_count)
        self.assertIn("timer_expired", self.provider.chat.call_args.args[0][0]["content"])
        self.assertEqual([], self.rows)

    async def test_unconfirmed_save_return_must_not_ack(self):
        for return_value in (False, None, 1):
            with mock.patch.object(self.session, "save_runtime_state", return_value=return_value):
                await self.reflect()
            self.assertIsNone(self.session.reflection_state)
        self.runtime.acknowledge_reflection_results.assert_not_called()


class TimerExpiryResultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.session = SimpleNamespace(id="owner", profile_id="profile-owner", display_name="Fixture",
                                       conversation_store=mock.Mock())
        self.manager = SimpleNamespace(get_by_id=mock.Mock(return_value=self.session),
                                       get_current=mock.Mock(return_value=self.session))
        self.runtime = SimpleNamespace(record_timer_expiry=mock.Mock())
        self.config = SimpleNamespace(get_active_llm_preset=lambda: {})
        self.app = SimpleNamespace(state=SimpleNamespace(session_manager=self.manager,
                                  agent_runtime=self.runtime, config=self.config))
        self.timer = {"id": "timer-1", "label": "tea", "session_id": "owner"}
        for name in ("log_debug", "log_error"):
            self.stack.enter_context(mock.patch("src.utils.debug_logger." + name))

    async def test_record_once_even_without_reminder_model(self):
        await _on_expire(self.app, self.timer)
        self.runtime.record_timer_expiry.assert_called_once_with(self.timer, "profile-owner")
        self.manager.get_current.assert_not_called()

    async def test_missing_explicit_owner_never_falls_back(self):
        self.manager.get_by_id.return_value = None
        await _on_expire(self.app, self.timer)
        self.manager.get_current.assert_not_called()
        self.runtime.record_timer_expiry.assert_not_called()

    async def test_empty_legacy_owner_retains_fallback(self):
        self.timer["session_id"] = ""
        await _on_expire(self.app, self.timer)
        self.manager.get_current.assert_called_once()
        self.runtime.record_timer_expiry.assert_called_once_with(self.timer, "profile-owner")

    async def test_record_failure_does_not_duplicate_or_prevent_existing_reminder(self):
        self.runtime.record_timer_expiry.side_effect = RuntimeError("store unavailable")
        self.config.get_active_llm_preset = lambda: {"base_url": "https://example.invalid"}
        async def stream(*args, **kwargs):
            yield "Tea is ready."
        provider = SimpleNamespace(stream_chat=mock.Mock(side_effect=stream))
        with mock.patch("src.llm.registry.get_provider", return_value=provider), \
             mock.patch("src.prompt.pipeline.build_messages", return_value=[{"role": "system", "content": "Fixture"}]), \
             mock.patch("src.config.effective_config.get_effective_max_history_turns", return_value=4), \
             mock.patch("src.core.broadcast.broadcast.push", new_callable=mock.AsyncMock):
            await _on_expire(self.app, self.timer)
        self.runtime.record_timer_expiry.assert_called_once()
        provider.stream_chat.assert_called_once()
        self.session.conversation_store.append.assert_called_once()


if __name__ == "__main__":
    unittest.main()
