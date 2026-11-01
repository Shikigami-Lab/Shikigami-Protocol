"""Synthetic, model-free reflection action acceptance and isolation."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.agent.autonomy import AutonomyRunner
from src.agent.runtime import AgentRuntime
from src.agent.settings import normalize_settings
from src.core.reflection import _parse_reflection_response


class AutonomyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sessions = {p: SimpleNamespace(id=p, profile_id=p) for p in ('a', 'b', 'c')}
        self.current = self.sessions['a']
        self.profiles = {p: {'tool_configs': {'agent': {'autonomy_enabled': True}}} for p in self.sessions}
        self.config = SimpleNamespace(agent={'enabled': True, 'workspace_enabled': True,
            'autonomy_enabled': True, 'autonomy_interval_seconds': 60,
            'autonomy_max_requests_per_hour': 12},
            get_active_llm_preset=lambda: {'base_url': 'http://localhost:9999/v1', 'model': 'fake'})
        self.app = SimpleNamespace(state=SimpleNamespace(config=self.config,
            session_manager=SimpleNamespace(get_current=lambda: self.current)))
        self.runtime = AgentRuntime(self.app, root=self.tmp.name, profile_loader=self.profiles.get)
        self.runner = AutonomyRunner(self.runtime)
        self.runtime.autonomy = self.runner
        self.started = []
        self.messages = [{'role': 'system', 'content': 'Synthetic character'}, {'role': 'user', 'content': 'Synthetic action'}]
        self.builder = patch.object(self.runner, '_build_messages', side_effect=lambda s, g: self.messages)
        self.builder.start()
        self.addCleanup(self.builder.stop)

        async def drive(run):
            self.started.append(run)
            directory = self.runtime.workspaces.directory(run.profile_id)
            (directory / 'note.txt').write_text(run.profile_id, encoding='utf-8')
            await self.runtime._publish(run, {'type': 'tool_call', 'tool_name': 'write', 'arguments': '{}'})
            await self.runtime._publish(run, {'type': 'completed'})
            await run.queue.put(None)
        self.drive = patch.object(self.runtime, '_drive', side_effect=drive)
        self.drive.start()
        self.addCleanup(self.drive.stop)

    async def asyncTearDown(self):
        await self.runner.close()
        await self.runtime.close()

    async def settle(self):
        await asyncio.gather(*list(self.runner.tasks))

    async def test_default_off_and_strict_settings(self):
        self.assertFalse(normalize_settings()['autonomy_enabled'])
        for value in ({'autonomy_enabled': 1}, {'autonomy_interval_seconds': 0}, {'autonomy_max_requests_per_hour': True}):
            with self.assertRaises(ValueError):
                normalize_settings(value)
        self.config.agent['autonomy_enabled'] = False
        self.profiles['a']['tool_configs']['agent']['autonomy_enabled'] = None
        self.assertFalse(await self.runner.start(self.current, 'write a note', 'cycle-1'))
        self.config.agent['autonomy_enabled'] = True
        self.profiles['a']['tool_configs']['agent']['autonomy_enabled'] = False
        self.assertFalse(await self.runner.start(self.current, 'write a note', 'cycle-1'))

    async def test_role_tool_master_switch_blocks_auto_and_running_calls(self):
        self.profiles['a']['tool_configs']['agent']['enabled'] = False
        self.assertFalse(await self.runner.start(self.current, 'write', 'cycle-1'))
        self.profiles['a']['tool_configs']['agent']['enabled'] = True
        self.assertTrue(await self.runner.start(self.current, 'write', 'cycle-2'))
        run = next(iter(self.runtime._runs.values()))
        self.profiles['a']['tool_configs']['agent']['enabled'] = False
        with self.assertRaises(ValueError):
            self.runtime._authorize(run)
        await self.runtime.cancel(run.id, 'a')
        await self.settle()

    async def test_three_roles_keep_files_origin_results_and_no_chat_writes(self):
        self.config.agent['autonomy_max_requests_per_hour'] = 18
        for profile in self.sessions:
            self.current = self.sessions[profile]
            self.assertTrue(await self.runner.start(self.current, 'write a note', 'cycle-1'))
            await self.settle()
            row = self.runtime.store.list_runs(profile)[0]
            self.assertEqual(row['origin'], 'reflection')
            self.assertIsNone(row.get('user_message_id'))
            self.assertEqual((self.runtime.workspaces.directory(profile) / 'note.txt').read_text(), profile)
            self.assertTrue(self.runtime.reflection_results(profile))
        self.assertEqual([r.profile_id for r in self.started], ['a', 'b', 'c'])

    async def test_group_inactive_foreground_and_receipts_do_not_launch(self):
        self.runner.group_view = True
        self.assertFalse(await self.runner.start(self.current, 'write', 'view'))
        self.runner.group_view = False
        group = SimpleNamespace(id='group', profile_id='a')
        self.assertFalse(await self.runner.start(group, 'write', 'g'))
        self.assertFalse(await self.runner.start(self.sessions['b'], 'write', 'b'))
        self.app.state.chat_streaming_sessions = {'a'}
        self.assertFalse(await self.runner.start(self.current, 'write', 'chat'))
        self.app.state.chat_streaming_sessions = set()
        self.assertFalse(await self.runner.start(self.current, 'write', 'receipt', from_tool_results=True))
        self.assertEqual(self.runtime.store.list_runs('a'), [])

    async def test_no_tool_outcome_reaches_reflection_without_timer_side_effects(self):
        async def drive(run):
            self.assertFalse(run.timer_enabled)
            with self.assertRaises(ValueError):
                await self.runtime.execute_timer(run.id, 'timer_create', {'seconds': 60, 'label': 'no'}, 'timer')
            await self.runtime._publish(run, {'type': 'reply_delta', 'text': 'Synthetic research conclusion'})
            await self.runtime._publish(run, {'type': 'completed'})
            await run.queue.put(None)
        with patch.object(self.runtime, '_drive', side_effect=drive):
            self.assertTrue(await self.runner.start(self.current, 'consider a topic', 'cycle-1'))
            await self.settle()
        result = self.runtime.reflection_results('a')[0]['payload']['result']
        self.assertEqual(result['outcome']['summary'], 'Synthetic research conclusion')
        self.assertEqual(result['outcome']['artifacts'], [])

    async def test_switch_preserves_owner_and_revocation_blocks_further_calls(self):
        self.assertTrue(await self.runner.start(self.current, 'write a note', 'cycle-1'))
        run = next(iter(self.runtime._runs.values()))
        self.current = self.sessions['b']
        self.runtime._authorize(run)
        self.assertEqual(run.envelope['origin'], 'reflection')
        self.assertEqual(run.envelope['profile_id'], 'a')
        self.assertFalse(self.runtime.store.get_run(run.id, 'b'))
        self.profiles['a']['tool_configs']['agent']['autonomy_enabled'] = False
        with self.assertRaises(ValueError):
            self.runtime._authorize(run)
        await self.runtime.cancel(run.id, 'a')
        await self.settle()

    async def test_cooldown_duplicate_and_budget_boundaries(self):
        store = self.runtime.store
        with patch('src.agent.link_store.time.time', return_value=10000):
            self.assertEqual(store.claim_autonomy('one', 'a', 'Write  a NOTE', 60, 12, 6), 6)
            self.assertEqual(store.claim_autonomy('two', 'a', 'different', 60, 12, 6), 0)
        with patch('src.agent.link_store.time.time', return_value=10061):
            self.assertEqual(store.claim_autonomy('three', 'a', 'write a note', 60, 12, 6), 0)
            self.assertEqual(store.claim_autonomy('four', 'b', 'write a note', 60, 12, 6), 6)
            self.assertEqual(store.claim_autonomy('five', 'c', 'other', 60, 12, 6), 0)
        with patch('src.agent.link_store.time.time', return_value=14000):
            self.assertEqual(store.claim_autonomy('six', 'c', 'other', 60, 12, 6), 6)

    async def test_duplicate_busy_and_global_budget_are_durable(self):
        self.config.agent['autonomy_max_requests_per_hour'] = 2
        self.assertTrue(await self.runner.start(self.current, 'write a note', 'cycle-1'))
        self.assertFalse(await self.runner.start(self.current, 'other', 'cycle-2'))
        await self.settle()
        self.assertEqual(self.started[0].gateway.max_requests, 2)
        self.current = self.sessions['b']
        self.assertFalse(await self.runner.start(self.current, 'other', 'cycle-3'))
        await self.runner.close()
        await self.runtime.close()
        self.runtime = AgentRuntime(self.app, root=self.tmp.name, profile_loader=self.profiles.get)
        self.runner = AutonomyRunner(self.runtime)
        self.runtime.autonomy = self.runner
        self.assertFalse(self.runtime.store.claim_autonomy('new', 'a', 'write a note', 60, 12, 6))

    async def test_optional_action_parser_rejects_objects_and_oversized_goals(self):
        for action, expected in [('write a note', 'write a note'), (None, ''), ({'tool': 'bash'}, ''), ('x' * 1001, '')]:
            result = await _parse_reflection_response(json.dumps({'thought': 'Synthetic', 'action': action}))
            self.assertEqual(result['action'], expected)

