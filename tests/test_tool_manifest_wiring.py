"""MANIFEST-driven tool wiring: register_tool imports command/segment modules.

Each tool's register(app) should only register its manifest; has_commands /
has_segment must wire the convention modules (or an explicitly declared
module, e.g. weather's segment living in src/prompt/segments/).
"""
import importlib
import sys
from unittest import TestCase

from src.commands.base import _COMMAND_REGISTRY
from src.prompt.registry import get_segment_class
from src.tools.registry import get_tool, register_tool


def _unload(*module_names):
    """Remove modules (and cached registrations they made) so wiring can be
    re-exercised from a clean import state."""
    for name in module_names:
        sys.modules.pop(name, None)
    _COMMAND_REGISTRY.pop("timer", None)
    _COMMAND_REGISTRY.pop("todo", None)
    _COMMAND_REGISTRY.pop("search", None)


class ManifestWiringTests(TestCase):
    def tearDown(self):
        # Re-import everything so later tests see the normal registered state.
        for name in ("src.tools.timer.commands", "src.tools.timer.segment",
                     "src.tools.todo.commands", "src.tools.todo.segment",
                     "src.tools.websearch.commands"):
            importlib.import_module(name)

    def test_timer_register_wires_commands_and_segment(self):
        _unload("src.tools.timer.commands", "src.tools.timer.segment")
        from src.tools import timer
        timer.register(None)
        self.assertIn("timer", _COMMAND_REGISTRY)
        self.assertIsNotNone(get_segment_class("active_timers"))

    def test_todo_register_wires_commands_and_segment(self):
        _unload("src.tools.todo.commands", "src.tools.todo.segment")
        from src.tools import todo
        todo.register(None)
        self.assertIn("todo", _COMMAND_REGISTRY)
        self.assertIsNotNone(get_segment_class("todos"))

    def test_websearch_register_wires_commands_only(self):
        _unload("src.tools.websearch.commands")
        from src.tools import websearch
        websearch.register(None)
        self.assertIn("search", _COMMAND_REGISTRY)
        self.assertFalse(websearch.MANIFEST.get("has_segment"))

    def test_weather_declares_external_segment_module(self):
        from src.tools import weather
        weather.register(None)
        self.assertEqual(weather.MANIFEST.get("segment_module"),
                         "src.prompt.segments.weather")
        self.assertIsNotNone(get_segment_class("weather"))

    def test_missing_convention_module_only_warns(self):
        register_tool({"tool_id": "unit_missing_wiring",
                       "has_commands": True, "has_segment": True})
        self.assertIsNotNone(get_tool("unit_missing_wiring"))

    def test_explicit_module_must_exist(self):
        with self.assertRaises(ImportError):
            register_tool({"tool_id": "unit_bad_wiring",
                           "has_segment": True,
                           "segment_module": "src.tools.no_such_module"})

    def test_agent_capable_flags_consistent(self):
        from src.tools import timer, todo, weather, websearch
        for mod in (timer, todo, weather, websearch):
            self.assertTrue(mod.MANIFEST.get("agent_capable"), mod.MANIFEST["tool_id"])
