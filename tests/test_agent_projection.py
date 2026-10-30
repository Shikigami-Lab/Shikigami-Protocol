import unittest
from src.agent.projection import project_arguments, safe_text, artifact_preview
from src.agent.link_store import _json

class ProjectionTests(unittest.TestCase):
    def test_file_payload_is_referenced_not_duplicated(self):
        value = project_arguments({'file_path': 'notes.txt', 'content': 'user supplied file', 'password': 'do-not-log'})
        self.assertEqual(value['file_path'], 'notes.txt')
        self.assertNotIn('content', value)
        self.assertNotIn('password', value)
        self.assertEqual(value['content_bytes'], 18)
        self.assertEqual(len(value['content_sha256']), 64)

    def test_credential_echo_is_redacted_without_failing_task(self):
        self.assertNotIn('sk-abcdefghijklmnop', safe_text('failed sk-abcdefghijklmnop'))

    def test_long_paths_and_arguments_fit_storage_without_mislabelled_paths(self):
        files = [{'path': ('目录/' * 12) + '字' * 220, 'size': 2, 'modified': 1} for _ in range(20)]
        preview, truncated = artifact_preview(files)
        self.assertTrue(truncated)
        self.assertEqual(preview, files[:len(preview)])
        _json({'result': {'artifacts': preview, 'artifacts_truncated': truncated}})
        value = project_arguments({'old_string': '字' * 2000, 'new_string': '文' * 2000})
        self.assertTrue(value['arguments_omitted'])
        _json({'result': {'arguments': value}})
