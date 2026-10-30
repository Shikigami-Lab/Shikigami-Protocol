import unittest
from types import SimpleNamespace
from src.agent.settings import normalize_settings
from src.agent.capabilities import capabilities, native_tools


class CapabilityTests(unittest.TestCase):
    def test_new_access_is_opt_in_and_code_requires_workspace(self):
        settings = normalize_settings({'enabled': True})
        self.assertFalse(settings['workspace_enabled'])
        self.assertEqual(native_tools(capabilities(settings, {})), [])
        with self.assertRaises(ValueError):
            normalize_settings({'code_enabled': True})

    def test_profile_can_restrict_but_not_grant_global_access(self):
        settings = normalize_settings({'workspace_enabled': True, 'code_enabled': True})
        c = capabilities(settings, {'tool_configs': {'agent': {'code_enabled': False, 'web_enabled': True}}})
        self.assertFalse(c['code_enabled'])
        self.assertFalse(c['web_enabled'])
        self.assertIn('write', native_tools(c))
        self.assertNotIn('bash', native_tools(c))

    def test_explicit_native_catalog(self):
        c = capabilities(normalize_settings({'workspace_enabled': True, 'code_enabled': True, 'web_enabled': True}), {})
        self.assertEqual(set(native_tools(c)), {'read', 'write', 'edit', 'glob', 'grep', 'bash', 'job_output', 'job_kill', 'web_fetch'})
