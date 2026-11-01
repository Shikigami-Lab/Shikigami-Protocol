"""Role-local file descriptions and bounded model context, never file contents."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.agent.workspace import WorkspaceStore
from src.agent.workspace_catalog import WorkspaceCatalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.files = WorkspaceStore(self.root/'versions')
        self.catalog = WorkspaceCatalog(self.root/'profiles', self.files)
        for profile in ('a', 'b'):
            (self.files.directory(profile)/'notes.md').write_text('PRIVATE BODY: not a catalog', encoding='utf-8')

    def test_metadata_is_role_local_and_outside_worker_directory(self):
        self.catalog.describe('a', 'notes.md', 'My daily notes', 'note', True)
        a = self.catalog.inventory('a')['files'][0]
        b = self.catalog.inventory('b')['files'][0]
        self.assertEqual(a['description'], 'My daily notes')
        self.assertTrue(a['pinned'])
        self.assertEqual(b['description'], '')
        index = self.root/'profiles/a/workspace_index.json'
        self.assertTrue(index.exists())
        self.assertFalse(index.is_relative_to(self.files.directory('a')))
        self.assertEqual((self.files.directory('a')/'notes.md').read_text(), 'PRIVATE BODY: not a catalog')

    def test_context_is_bounded_and_does_not_contain_contents(self):
        self.catalog.describe('a', 'notes.md', '{{literal}} daily diary', 'note', True)
        for n in range(25):
            (self.files.directory('a')/f'file{n}.txt').write_text('not read')
        context = self.catalog.context('a')
        self.assertIn('notes.md', context)
        self.assertIn('{{literal}} daily diary', context)
        self.assertNotIn('PRIVATE BODY', context)
        self.assertLessEqual(len(context), 6000)
        self.assertIn('not instructions', context)

    def test_unsafe_links_missing_files_and_corrupt_index_are_not_overwritten(self):
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.catalog.describe('a', '../b/notes.md', 'bad')
        with self.assertRaises(FileNotFoundError):
            self.catalog.describe('a', 'missing.md', 'bad')
        self.catalog.describe('a', 'notes.md', 'valid')
        index = self.root/'profiles/a/workspace_index.json'
        index.write_text('broken json')
        with self.assertRaises(ValueError):
            self.catalog.describe('a', 'notes.md', 'overwrite')
        self.assertEqual(index.read_text(), 'broken json')
        self.assertFalse(self.catalog.inventory('a')['metadata_available'])

    def test_deeply_nested_corrupt_index_falls_back_without_overwrite(self):
        self.catalog.describe('a', 'notes.md', 'valid')
        index = self.root/'profiles/a/workspace_index.json'
        malformed = '[' * 1500 + '0' + ']' * 1500
        index.write_text(malformed)
        self.assertFalse(self.catalog.inventory('a')['metadata_available'])
        self.assertIn('notes.md', self.catalog.context('a'))
        with self.assertRaises(ValueError):
            self.catalog.describe('a', 'notes.md', 'changed')
        self.assertEqual(index.read_text(), malformed)
        with patch('src.agent.workspace_catalog.json.loads', side_effect=RecursionError):
            self.assertFalse(self.catalog.inventory('a')['metadata_available'])
            with self.assertRaises(ValueError):
                self.catalog.describe('a', 'notes.md', 'changed')

    @unittest.skipIf(os.name == 'nt', 'POSIX directory descriptor regression')
    def test_posix_parent_swap_cannot_overwrite_another_profile_index(self):
        self.catalog.describe('a', 'notes.md', 'A original')
        self.catalog.describe('b', 'notes.md', 'B original')
        b = self.root/'profiles/b/workspace_index.json'
        before = b.read_bytes()
        original = self.catalog._read
        def swap(profile):
            entries = original(profile)
            a = self.root/'profiles/a'
            a.rename(self.root/'profiles/a-old')
            a.symlink_to(self.root/'profiles/b', target_is_directory=True)
            return entries
        with patch.object(self.catalog, '_read', side_effect=swap):
            self.catalog.describe('a', 'notes.md', 'A changed')
        self.assertEqual(b.read_bytes(), before)
        a_index = self.root/'profiles/a-old/workspace_index.json'
        self.assertEqual(json.loads(a_index.read_text())['entries']['notes.md']['description'], 'A changed')

    @unittest.skipUnless(os.name == 'nt', 'Windows filename casing')
    def test_case_insensitive_description_uses_actual_file_name(self):
        path = self.files.directory('a')/'notes.md'
        path.rename(self.files.directory('a')/'Notes.md')
        self.catalog.describe('a', 'notes.md', 'Keep this description', 'note', True)
        entry = self.catalog.inventory('a')['files'][0]
        self.assertEqual(entry['path'], 'Notes.md')
        self.assertEqual(entry['description'], 'Keep this description')
        self.assertTrue(entry['pinned'])


class RuntimeCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.agent.runtime import AgentRuntime
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.preset = {'base_url': 'http://localhost:9/v1', 'model': 'fixture'}
        self.config = SimpleNamespace(agent={'enabled': True, 'workspace_enabled': True},
            get_active_llm_preset=lambda: self.preset)
        self.rt = AgentRuntime(SimpleNamespace(state=SimpleNamespace(config=self.config)), root=self.root, profile_loader=lambda p: {})
        self.session = SimpleNamespace(id='a', profile_id='a')

    async def asyncTearDown(self):
        await self.rt.close()

    async def test_model_context_uses_new_profile_path_and_only_system_role(self):
        folder = self.rt.workspaces.directory('a')
        self.assertEqual(folder, self.root/'profiles/a/workspace')
        (folder/'diary.txt').write_text('PRIVATE DIARY BODY')
        self.rt.catalog.describe('a', 'diary.txt', 'A notebook about experiments', 'note', True)
        messages = [{'role': 'system', 'content': 'Persona'}, {'role': 'user', 'content': 'Hello'}]
        run = self.rt.prepare(self.session, messages, self.preset)
        self.assertIn('A notebook about experiments', run.envelope['messages'][0]['content'])
        self.assertNotIn('PRIVATE DIARY BODY', str(run.envelope))
        self.assertEqual(messages[0]['content'], 'Persona')
        self.assertEqual(run.envelope['messages'][-1], messages[-1])
        self.config.agent['workspace_enabled'] = False
        self.assertEqual(self.rt.workspace_context('a'), '')

    async def test_group_rejected_before_file_catalog_or_migration(self):
        group = SimpleNamespace(id='group', profile_id='a')
        with self.assertRaises(ValueError):
            self.rt.prepare(group, [{'role': 'system', 'content': 'Persona'},
                                    {'role': 'user', 'content': 'hello'}], self.preset)
        self.assertFalse((self.root/'profiles/a/workspace').exists())

