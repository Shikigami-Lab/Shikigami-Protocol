"""Synthetic profile-bound todo tools; no model, real notes or private data."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.agent.runtime import AgentRuntime
from src.tools.todo.store import TodoStore


class AgentTodoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.sessions = {p: SimpleNamespace(id=p, profile_id=p, storage_root=str(root/p)) for p in ('a', 'b')}
        self.current = self.sessions['a']
        self.profile = {'tool_configs': {'agent': {'autonomy_enabled': True}}}
        self.preset = {'base_url': 'http://localhost:9/v1', 'model': 'fake'}
        self.config = SimpleNamespace(agent={'enabled': True, 'workspace_enabled': True,
            'todo_enabled': True, 'autonomy_enabled': True}, get_active_llm_preset=lambda: self.preset)
        app = SimpleNamespace(state=SimpleNamespace(config=self.config,
            session_manager=SimpleNamespace(get_current=lambda: self.current)))
        self.runtime = AgentRuntime(app, root=root, profile_loader=lambda _: self.profile)
        self.messages = [{'role': 'system', 'content': 'Synthetic'}, {'role': 'user', 'content': 'Synthetic request'}]
        self.run = self.runtime.prepare(self.current, self.messages, self.preset)

    async def asyncTearDown(self):
        await self.runtime.close()

    async def tool(self, name, args, request='1'):
        return await self.runtime.execute_tool(self.run.id, name, args, request)

    async def test_add_deduplicates_and_preserves_notebook(self):
        note = self.runtime.workspaces.directory('a')/'fox_ledger.md'
        note.write_text('Synthetic notebook; never rewrite this.', encoding='utf-8')
        before = note.read_bytes()
        args = {'content': 'Draw a fox', 'owner': 'ai', 'source_path': 'fox_ledger.md'}
        first = await self.tool('todo_add', args)
        self.assertEqual(await self.tool('todo_add', args), first)
        items = TodoStore(self.current.storage_root).get_all()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].owner, 'ai')
        self.assertEqual(items[0].last_run_id, self.run.id)
        self.assertEqual(TodoStore(self.sessions['b'].storage_root).get_all(), [])
        self.assertEqual(note.read_bytes(), before)
        with self.assertRaises(ValueError):
            await self.tool('todo_add', {**args, 'content': 'different'}, '1')

    async def test_credential_like_note_name_does_not_break_receipt_after_write(self):
        name = 'sk-research-notebook.md'
        note = self.runtime.workspaces.directory('a') / name
        note.write_text('Synthetic note')
        result = await self.tool('todo_add', {'content': 'Plan', 'owner': 'ai', 'source_path': name})
        self.assertTrue(result['ok'])
        self.assertEqual(TodoStore(self.current.storage_root).get_by_id(result['todo_id']).source_path, name)
        self.assertEqual(await self.tool('todo_add', {'content': 'Plan', 'owner': 'ai', 'source_path': name}), result)

    async def test_real_id_complete_with_result_link(self):
        result = await self.tool('todo_add', {'content': 'Draw a fox', 'owner': 'ai'})
        path = self.runtime.workspaces.directory('a')/'drawing.txt'
        path.write_text('Synthetic finished drawing')
        await self.tool('todo_update', {'todo_id': result['todo_id'], 'done': True, 'evidence_path': 'drawing.txt'}, '2')
        item = TodoStore(self.current.storage_root).get_by_id(result['todo_id'])
        self.assertTrue(item.done)
        self.assertEqual(item.evidence_path, 'drawing.txt')
        with self.assertRaises(ValueError):
            await self.tool('todo_update', {'todo_id': result['todo_id'], 'evidence_path': '../outside'}, '3')

    async def test_autonomous_tools_cannot_modify_user_todos(self):
        user = TodoStore(self.current.storage_root).add('User responsibility')
        await self.runtime.cancel(self.run.id, 'a')
        self.run = self.runtime.prepare(self.current, self.messages, self.preset, origin='reflection')
        for name, args in [('todo_update', {'todo_id': user.id, 'done': True}),
                           ('todo_delete', {'todo_id': user.id}), ('todo_add', {'content': 'new', 'owner': 'user'})]:
            with self.assertRaises(ValueError):
                await self.tool(name, args, name)
        self.assertFalse(TodoStore(self.current.storage_root).get_by_id(user.id).done)
        result = await self.tool('todo_add', {'content': 'Own project'}, 'own')
        self.assertEqual(TodoStore(self.current.storage_root).get_by_id(result['todo_id']).owner, 'ai')

    async def test_disabled_tool_and_strict_arguments(self):
        for args in ({'content': 'x', 'profile_id': 'b'}, {'content': ''}, {'content': 'x', 'priority': True}):
            with self.assertRaises(ValueError):
                await self.tool('todo_add', args)
        self.config.agent['todo_enabled'] = False
        with self.assertRaises(ValueError):
            await self.tool('todo_add', {'content': 'blocked'})

    async def test_reflection_gets_only_own_pending_plans_without_reading_notebook(self):
        from src.agent.todo_tools import reflection_plan_context
        store = TodoStore(self.current.storage_root)
        store.add('Private user responsibility')
        plan = store.add('Own unfinished drawing', owner='ai')
        done = store.add('Finished idea', owner='ai')
        store.update(done.id, done=True)
        context = reflection_plan_context(self.runtime, self.current)
        self.assertIn('Own unfinished drawing', context)
        self.assertIn(plan.id, context)
        self.assertNotIn('Private user responsibility', context)
        self.assertNotIn('Finished idea', context)
        self.assertNotIn('Own unfinished drawing', reflection_plan_context(self.runtime, self.sessions['b']))
        self.config.agent['enabled'] = False
        self.assertEqual(reflection_plan_context(self.runtime, self.current), '')
        self.config.agent['enabled'] = True
        self.config.agent['todo_enabled'] = False
        self.assertEqual(reflection_plan_context(self.runtime, self.current), '')

    async def test_permission_revocation_and_switch_keep_original_owner(self):
        self.current = self.sessions['b']
        created = await self.tool('todo_add', {'content': 'Belongs to a'})
        self.assertIsNotNone(TodoStore(self.sessions['a'].storage_root).get_by_id(created['todo_id']))
        self.assertEqual(TodoStore(self.sessions['b'].storage_root).get_all(), [])
        self.profile['tool_configs']['todo'] = {'enabled': False}
        with self.assertRaises(ValueError):
            await self.tool('todo_delete', {'todo_id': created['todo_id']}, '2')


class TodoMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_catalog_and_write_annotations(self):
        from unittest.mock import AsyncMock
        from tests.test_agent_business_tools import business_client
        from src.agent.todo_tools import TODO_TOOLS
        callback = AsyncMock(return_value={'ok': True})
        async with business_client(callback, TODO_TOOLS) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            for name in TODO_TOOLS:
                self.assertFalse(tools[name].inputSchema['additionalProperties'])
                self.assertEqual(tools[name].annotations.readOnlyHint, name == 'todo_list')
            self.assertTrue(tools['todo_delete'].annotations.destructiveHint)
            self.assertFalse((await client.call_tool('todo_add', {'content': 'Synthetic plan', 'owner': 'ai'})).isError)
            self.assertEqual(callback.await_args.args[0], 'todo_add')
            self.assertEqual(callback.await_args.args[1]['owner'], 'ai')
            before = callback.await_count
            self.assertTrue((await client.call_tool('todo_add', {'content': 'x', 'priority': True})).isError)
            self.assertTrue((await client.call_tool('todo_add', {'content': 'x', 'profile_id': 'other'})).isError)
            self.assertEqual(callback.await_count, before)

