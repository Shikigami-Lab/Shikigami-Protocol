"""Agent business-tool registry: registration, dispatch and capability gating."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase

from pydantic import BaseModel, Field

from src.agent.tool_registry import (
    BusinessToolSpec, all_business_tools, ensure_builtin_tools, get_business_tool,
    register_business_tool, _REGISTRY,
)


class RegistryMechanicsTests(TestCase):
    def tearDown(self):
        _REGISTRY.pop("unit_test_tool", None)

    def _spec(self, name="unit_test_tool"):
        class Args(BaseModel):
            value: int = Field(strict=True, ge=0, le=9)

        async def handler(app, session, args):
            return {"ok": True, "value": args.value}

        return BusinessToolSpec(
            name=name, args_model=Args, handler=handler,
            capability="unit_test_enabled",
            wrapper_factory=lambda call_tool: None,
        )

    def test_register_and_lookup(self):
        spec = self._spec()
        register_business_tool(spec)
        self.assertIs(_REGISTRY["unit_test_tool"], spec)

    def test_duplicate_registration_rejected(self):
        register_business_tool(self._spec())
        with self.assertRaises(ValueError):
            register_business_tool(self._spec())

    def test_invalid_specs_rejected(self):
        with self.assertRaises(ValueError):
            register_business_tool(self._spec(name=""))
        with self.assertRaises(ValueError):
            register_business_tool(self._spec(name="todo_list"))
        spec = self._spec()
        object.__setattr__(spec, "args_model", dict)
        with self.assertRaises(ValueError):
            register_business_tool(spec)


class BuiltinRegistrationTests(TestCase):
    def test_builtins_register_in_stable_order(self):
        ensure_builtin_tools()
        names = [spec.name for spec in all_business_tools()]
        self.assertEqual(names, ["history_search", "history_context", "memory_search",
                                 "web_search", "weather_query"])

    def test_builtin_capabilities_and_annotations(self):
        expected = {"history_search": ("history_enabled", False),
                    "history_context": ("history_enabled", False),
                    "memory_search": ("memory_enabled", False),
                    "web_search": ("web_enabled", True),
                    "weather_query": ("weather_enabled", True)}
        for spec in all_business_tools():
            capability, open_world = expected[spec.name]
            self.assertEqual(spec.capability, capability)
            self.assertEqual(spec.open_world, open_world)
            self.assertTrue(spec.read_only)
            self.assertFalse(spec.destructive)
        weather = get_business_tool("weather_query")
        self.assertIsNotNone(weather.capability_resolver)
        profile = {"tool_configs": {"weather": {"enabled": True}}}
        self.assertTrue(weather.capability_resolver({}, profile))
        self.assertFalse(weather.capability_resolver({}, {}))


class RegistryDispatchTests(IsolatedAsyncioTestCase):
    async def test_dispatch_and_unknown_and_validation(self):
        from src.agent.business_tools import call_business_tool

        session = SimpleNamespace(profile_id="p1", id="p1", storage_root=".")
        bad = await call_business_tool(None, session, "nope", {})
        self.assertEqual(bad["error"]["code"], "unknown_tool")
        invalid = await call_business_tool(None, session, "history_search", {"limit": 999})
        self.assertEqual(invalid["error"]["code"], "invalid_arguments")
        unbound = await call_business_tool(None, SimpleNamespace(profile_id="p1", id="x"),
                                           "history_search", {})
        self.assertEqual(unbound["error"]["code"], "invalid_session")

    async def test_capability_gating_uses_registry(self):
        from src.agent.capabilities import business_tools, capabilities

        access = capabilities({"history_enabled": True, "web_enabled": True}, {})
        tools = business_tools(access)
        self.assertIn("history_search", tools)
        self.assertIn("web_search", tools)
        self.assertNotIn("weather_query", tools)
        profile = {"tool_configs": {"weather": {"enabled": True}}}
        self.assertIn("weather_query", business_tools(capabilities({}, profile)))
