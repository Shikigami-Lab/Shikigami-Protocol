import unittest
from types import SimpleNamespace
from src.agent.settings import normalize_settings
from src.agent.capabilities import capabilities, native_tools, business_tools, capability_context


class CapabilityTests(unittest.TestCase):
    def test_explicit_persona_capability_overrides_global_defaults(self):
        for default in (False, True):
            for override in (None, False, True):
                settings = normalize_settings({'enabled': default, 'workspace_enabled': default})
                profile = {'tool_configs': {'agent': {'workspace_enabled': override}}}
                expected = default if override is None else override
                self.assertEqual(capabilities(settings, profile)['workspace_enabled'], expected)
    def test_character_tool_master_switch_restricts_all_capabilities(self):
        settings = normalize_settings(dict.fromkeys(('workspace_enabled', 'code_enabled', 'web_enabled', 'history_enabled'), True))
        profile = {'tool_configs': {'agent': {'enabled': False}}}
        self.assertFalse(any(capabilities(settings, profile).values()))

    def test_prompt_uses_registered_mcp_names(self):
        access = dict.fromkeys(('workspace_enabled', 'code_enabled', 'web_enabled', 'history_enabled'), True)
        prompt = capability_context(access)
        catalog = prompt.split('Available capabilities: ', 1)[1].split('. ', 1)[0].split(', ')
        self.assertEqual(catalog, native_tools(access) + [
            'mcp__shikigami__' + name for name in business_tools(access)])
        self.assertIn('use mcp__shikigami__task_context before acting', prompt)
        self.assertNotIn('use task_context before acting', prompt)

    def test_new_access_is_opt_in_and_code_requires_workspace(self):
        settings = normalize_settings({'enabled': True})
        self.assertFalse(settings['workspace_enabled'])
        self.assertEqual(native_tools(capabilities(settings, {})), [])
        with self.assertRaises(ValueError):
            normalize_settings({'code_enabled': True})

    def test_profile_can_override_global_defaults(self):
        settings = normalize_settings({'workspace_enabled': True, 'code_enabled': True})
        c = capabilities(settings, {'tool_configs': {'agent': {'code_enabled': False, 'web_enabled': True}}})
        self.assertFalse(c['code_enabled'])
        self.assertTrue(c['web_enabled'])
        self.assertIn('write', native_tools(c))
        self.assertNotIn('bash', native_tools(c))

    def test_explicit_native_catalog(self):
        c = capabilities(normalize_settings({'workspace_enabled': True, 'code_enabled': True, 'web_enabled': True}), {})
        self.assertEqual(set(native_tools(c)), {'read', 'write', 'edit', 'glob', 'grep', 'bash', 'job_output', 'job_kill', 'web_fetch'})
