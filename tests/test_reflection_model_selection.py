"""Reflection model routing with synthetic presets and no provider calls."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from src.config.app_config import AppConfig
from src.core.reflection import ReflectionEngine


class ReflectionModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.config = AppConfig(
            default_llm="chat",
            llm_presets={"chat": {"model": "chat-model"}, "other": {"model": "other-model"}},
        )
        self.engine = ReflectionEngine(SimpleNamespace(state=SimpleNamespace(config=self.config)))

    def configure(self, primary, fallback, enabled=True):
        self.config.secondary_models["reflection"] = {
            "enabled": enabled, "primary_preset": primary, "fallback_preset": fallback,
        }

    def test_active_primary_follows_current_chat_preset(self):
        self.configure("__active__", "other")
        for name in ("chat", "other"):
            with self.subTest(name=name):
                self.config.default_llm = name
                preset = self.config.llm_presets[name]
                self.assertEqual(self.engine._get_preset(), (preset, False, preset["model"]))
                self.assertIs(self.config.get_reflection_model_preset(), preset)

    def test_active_fallback_is_available_to_the_retry_path(self):
        self.configure("other", "__active__")
        self.assertEqual(self.engine._get_preset(), (self.config.llm_presets["other"], False, "other-model"))
        self.assertIs(self.engine._get_fallback_preset(), self.config.llm_presets["chat"])

    def test_active_fallback_is_selected_when_primary_missing_or_unset(self):
        for primary in ("deleted", "__none__", ""):
            with self.subTest(primary=primary):
                self.configure(primary, "__active__")
                self.assertEqual(self.engine._get_preset(), (self.config.llm_presets["chat"], True, "chat-model"))

    def test_dedicated_model_off_uses_only_active_chat_preset(self):
        self.configure("__none__", "other", enabled=False)
        self.assertEqual(self.engine._get_preset(), (self.config.llm_presets["chat"], False, "chat-model"))
        self.assertIsNone(self.engine._get_fallback_preset())
        self.assertIs(self.config.get_reflection_model_preset(), self.config.llm_presets["chat"])
        self.config.default_llm = "deleted"
        self.assertEqual(self.engine._get_preset(), (None, False, ""))
        self.assertIsNone(self.config.get_reflection_model_preset())

    def test_missing_active_primary_still_uses_named_fallback(self):
        self.configure("__active__", "other")
        self.config.default_llm = "deleted"
        self.assertEqual(self.engine._get_preset(), (self.config.llm_presets["other"], True, "other-model"))
        self.assertIs(self.config.get_reflection_model_preset(), self.config.llm_presets["other"])

    def test_dedicated_mode_does_not_implicitly_use_chat_for_unset_slots(self):
        self.configure("__none__", "")
        self.assertEqual(self.engine._get_preset(), (None, False, ""))
        self.assertIsNone(self.engine._get_fallback_preset())
        self.assertIsNone(self.config.get_reflection_model_preset())

    def test_active_model_routing_respects_global_gate_and_profile_opt_out(self):
        self.configure("__active__", "__none__")
        session = SimpleNamespace(id="fixture", profile_id="fixture", reflection_state=None)
        self.engine._app.state.session_manager = SimpleNamespace(
            get_current=lambda: session, list_sessions=lambda: [session],
        )
        for global_enabled, profile_enabled, expected_calls in (
            (False, True, 0), (True, False, 0), (True, True, 1),
        ):
            with self.subTest(global_enabled=global_enabled, profile_enabled=profile_enabled):
                self.config.reflection["enabled"] = global_enabled
                card = {"engine_overrides": {"reflection": {"enabled": profile_enabled}}}
                with (
                    mock.patch("src.config.effective_config._load_profile_card", return_value=card),
                    mock.patch("src.core.reflection.asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
                    mock.patch.object(self.engine, "_reflect") as reflect,
                    self.assertRaises(asyncio.CancelledError),
                ):
                    asyncio.run(self.engine._loop())
                self.assertEqual(reflect.await_count, expected_calls)
