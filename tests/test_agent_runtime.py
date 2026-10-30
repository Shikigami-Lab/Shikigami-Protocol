"""Host authority/receipts tests use temporary state and the real TimerManager."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.agent.runtime import AgentRuntime
from src.tools.timer.manager import TimerManager


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for name in ('log_timer', 'log_tool_timer'):
            guard = patch('src.tools.timer.manager.' + name)
            guard.start()
            self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = SimpleNamespace(agent={"enabled": True},
            get_active_llm_preset=lambda: {"base_url": "http://localhost:9999/v1", "model": "fixture"})
        self.app = SimpleNamespace(state=SimpleNamespace(config=self.config))
        self.timers = TimerManager()
        self.runtime = AgentRuntime(self.app, root=self.tmp.name, timer_manager=self.timers,
                                    profile_loader=lambda _: {"tool_configs": {"timer": {"enabled": True}}})
        self.session = SimpleNamespace(profile_id="a", id="a")
        self.run = self.runtime.prepare(self.session, [{"role": "system", "content": "fixture"},
            {"role": "user", "content": "test"}], self.config.get_active_llm_preset())

    async def asyncTearDown(self):
        await self.runtime.close()
        for timer in self.timers.list_timers():
            self.timers.stop_timer(timer['id'])

    async def test_timer_create_receipt_deduplicates_and_isolated_list_cancel(self):
        args = {"seconds": 600, "label": "synthetic"}
        first = await self.runtime.execute_timer(self.run.id, "timer_create", args, "request-1")
        again = await self.runtime.execute_timer(self.run.id, "timer_create", args, "request-1")
        self.assertEqual(first, again)
        self.assertEqual(len(self.timers.list_timers()), 1)
        self.timers.start_timer(600, "other", "b")
        listed = await self.runtime.execute_timer(self.run.id, "timer_list", {}, "request-2")
        self.assertEqual([t['id'] for t in listed['timers']], [first['timer_id']])
        other = next(t for t in self.timers.list_timers() if t['session_id'] == 'b')
        with self.assertRaises(ValueError):
            await self.runtime.execute_timer(self.run.id, "timer_cancel", {"timer_id": other['id']}, "request-3")
        await self.runtime.execute_timer(self.run.id, "timer_cancel", {"timer_id": first['timer_id']}, "request-4")
        self.assertEqual(len(self.timers.list_timers()), 1)
        self.assertTrue(self.runtime.reflection_results('a'))

    async def test_cancelled_invalidated_and_disabled_requests_cannot_execute(self):
        await self.runtime.invalidate('a')
        with self.assertRaises(ValueError):
            await self.runtime.execute_timer(self.run.id, 'timer_create', {'seconds': 600, 'label': 'no'}, 'late')
        self.assertFalse(self.timers.list_timers())
        self.assertFalse(self.runtime.store.is_current(self.run.id, 'a'))

    async def test_global_and_role_permissions_are_rechecked_on_host(self):
        self.config.agent['enabled'] = False
        with self.assertRaises(ValueError):
            await self.runtime.execute_timer(self.run.id, 'timer_list', {}, 'off')
        self.config.agent['enabled'] = True
        self.runtime.profile_loader = lambda _: {'tool_configs': {'timer': {'enabled': False}}}
        with self.assertRaises(ValueError):
            await self.runtime.execute_timer(self.run.id, 'timer_create', {'seconds': 600, 'label': 'no'}, 'role-off')

    async def test_request_nonce_and_scope_are_fixed_before_streaming(self):
        with self.assertRaises(ValueError):
            self.runtime.prepare(self.session, self.run.envelope['messages'], self.config.get_active_llm_preset())
        self.assertFalse(await self.runtime.cancel(self.run.id, 'b'))
        self.assertTrue(await self.runtime.cancel(self.run.id, 'a'))
        self.assertEqual(self.runtime.store.get_run(self.run.id)['state'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
