"""Per-persona opt-in is authoritative even when the global default is off."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from src.agent.runtime import AgentRuntime


class OverrideTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.role = {'tool_configs': {'agent': {}}}
        self.session = SimpleNamespace(id='a', profile_id='a', storage_root=self.tmp.name)
        self.config = SimpleNamespace(agent={'enabled': False, 'autonomy_enabled': False,
            'workspace_enabled': False}, get_active_llm_preset=lambda: {'base_url': 'http://localhost:9/v1', 'model': 'fixture'})
        self.app = SimpleNamespace(state=SimpleNamespace(config=self.config,
            session_manager=SimpleNamespace(get_current=lambda: self.session)))
        self.rt = AgentRuntime(self.app, root=self.tmp.name, profile_loader=lambda p: self.role)
        self.messages = [{'role': 'system', 'content': 'Synthetic'}, {'role': 'user', 'content': 'Hello'}]

    async def asyncTearDown(self):
        await self.rt.close()

    async def test_master_and_autonomy_truth_tables(self):
        for default in (False, True):
            for override in (None, False, True):
                self.config.agent.update(enabled=default, autonomy_enabled=default)
                self.role['tool_configs']['agent'] = {'enabled': override, 'autonomy_enabled': override}
                expected = default if override is None else override
                self.assertEqual(self.rt.tools_allowed('a'), expected)
                self.assertEqual(self.rt.autonomy_allowed('a'), expected)
                self.assertEqual(self.rt.autonomy.enabled(self.session), expected)

    async def test_global_off_persona_on_prepares_and_authorizes(self):
        self.role['tool_configs']['agent'] = {'enabled': True, 'autonomy_enabled': True, 'workspace_enabled': True}
        run = self.rt.prepare(self.session, self.messages, self.config.get_active_llm_preset(), origin='reflection')
        self.rt._authorize(run)
        self.assertTrue(run.access['workspace_enabled'])
        (self.rt.workspaces.directory('a')/'note.txt').write_text('Synthetic note')
        self.assertIn('note.txt', self.rt.workspace_context('a'))

    async def test_default_changes_do_not_revoke_explicit_on_but_persona_off_does(self):
        self.config.agent.update(enabled=True, autonomy_enabled=True, workspace_enabled=True)
        self.role['tool_configs']['agent'] = {'enabled': True, 'autonomy_enabled': True, 'workspace_enabled': True}
        run = self.rt.prepare(self.session, self.messages, self.config.get_active_llm_preset(), origin='reflection')
        self.config.agent.update(enabled=False, autonomy_enabled=False, workspace_enabled=False)
        self.rt._authorize(run)
        self.role['tool_configs']['agent']['autonomy_enabled'] = False
        with self.assertRaises(ValueError):
            self.rt._authorize(run)

    async def test_inherit_and_explicit_off_remain_denied(self):
        for value in (None, False):
            self.role['tool_configs']['agent']['enabled'] = value
            with self.assertRaises(ValueError):
                self.rt.prepare(self.session, self.messages, self.config.get_active_llm_preset())
        self.config.agent['enabled'] = True
        self.assertFalse(self.rt.tools_allowed('a'))
