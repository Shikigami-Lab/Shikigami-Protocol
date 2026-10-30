import unittest
from types import SimpleNamespace

from src.utils.persona_context import get_persona_context_for_secondary_llm, resolve_user_name


def _app(user_name="用户", user_persona=None):
    config = SimpleNamespace(user_name=user_name, user_persona=user_persona or {})
    return SimpleNamespace(state=SimpleNamespace(config=config))


class ResolveUserNameTests(unittest.TestCase):
    def test_profile_then_global_persona_then_config(self):
        app = _app(user_persona={"name": "Global"})
        self.assertEqual("Local", resolve_user_name({"user_persona": {"name": "Local"}}, app))
        self.assertEqual("Global", resolve_user_name({}, app))
        self.assertEqual("用户", resolve_user_name({}, _app()))
        self.assertEqual("", resolve_user_name({}, None))

    def test_helper_persona_context_prefers_the_profile_name(self):
        profile = {
            "display_name": "Mochi",
            "user_persona": {"name": "Alex"},
            "reflection_config": {"custom_prompt": "{{char}} teases {{user}}."},
        }
        self.assertEqual("Mochi teases Alex.", get_persona_context_for_secondary_llm(profile, user_name="用户"))


if __name__ == "__main__":
    unittest.main()
