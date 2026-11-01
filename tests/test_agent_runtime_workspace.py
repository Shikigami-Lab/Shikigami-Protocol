import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from src.agent.runtime import AgentRuntime


class RuntimeWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.preset = {'base_url': 'http://fixture.invalid/v1', 'model': 'fixture'}
        config = SimpleNamespace(agent={'enabled': True, 'workspace_enabled': True},
                                 get_active_llm_preset=lambda: self.preset)
        self.runtime = AgentRuntime(SimpleNamespace(state=SimpleNamespace(config=config)),
            root=self.temp.name, profile_loader=lambda _: {})
        self.session = SimpleNamespace(id='test', profile_id='test')
        self.messages = [{'role': 'system', 'content': 'fixture'}, {'role': 'user', 'content': 'work'}]
        self.run = self.runtime.prepare(self.session, self.messages, self.preset)

    async def asyncTearDown(self):
        await self.runtime.close()
        self.temp.cleanup()

    async def test_large_inventory_does_not_prevent_terminal_persistence(self):
        folder = self.runtime.workspaces.directory('test')
        for n in range(101):
            (folder / f'{n}.txt').write_text('small')
        await self.runtime._publish(self.run, {'type': 'completed'})
        row = self.runtime.store.get_run(self.run.id)
        self.assertEqual(row['state'], 'completed')
        result = row['events'][-1]['result']
        self.assertEqual(len(result['artifacts']), 20)
        self.assertTrue(result['artifacts_truncated'])
        self.assertEqual(len(self.runtime.workspaces.list_files('test')), 101)

    async def test_persistent_cleanup_marker_removed_only_after_confirmed_cleanup(self):
        self.runtime._command(self.run)
        self.assertEqual(len(self.runtime.cleanup_pending('test')), 1)
        with patch('src.agent.runtime.subprocess.run', return_value=SimpleNamespace(returncode=1, stderr=b'daemon unavailable')):
            with self.assertRaises(RuntimeError): self.runtime._remove_container(self.run)
        self.assertEqual(len(self.runtime.cleanup_pending('test')), 1)
        with patch('src.agent.runtime.subprocess.run', return_value=SimpleNamespace(returncode=1, stderr=b'No such container')) as remove:
            self.runtime._remove_container(self.run)
        self.assertIn('shikigami-agent-' + self.run.id, remove.call_args.args[0])
        self.assertEqual(self.runtime.cleanup_pending('test'), [])

    async def test_native_mount_does_not_expose_original_data(self):
        command = self.runtime._command(self.run)
        mounts = [command[i+1] for i,x in enumerate(command) if x=='--mount']
        self.assertTrue(any('target=/workspace' in m for m in mounts))
        self.assertFalse(any('docker.sock' in m for m in mounts))
        workspace_mounts = [m for m in mounts if 'target=/workspace' in m]
        self.assertEqual(len(workspace_mounts), 1)
        self.assertIn('source=' + str(self.runtime.root/'profiles/test/workspace'), workspace_mounts[0])
        self.assertFalse(any('source=' + str(self.runtime.root/'profiles') + ',' in m for m in mounts))
        self.assertFalse(any('todos.json' in m or 'workspace_index.json' in m for m in mounts))
        self.assertEqual(command[command.index('--network')+1], 'none')
