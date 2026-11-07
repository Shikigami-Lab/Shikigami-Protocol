import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import settings_ext
from src.config.app_config import AppConfig
from src.memory.conversation_store import Message
from src.memory.memory_manager import MemoryManager
from src.prompt.segments.persona import PersonaSegment


class SharedMemoryPromptsTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.config = AppConfig(default_llm="fixture", llm_presets={"fixture": {"model": "fixture"}},
                                memory={"day_summary_enabled": True}, user_name="Human")
        self.app = FastAPI()
        self.app.state.config = self.config
        self.app.include_router(settings_ext.router)
        self.config_path = self.root / "app.yaml"
        self.config_path.write_text("memory:\n  day_summary_enabled: true\n  extraction_frequency: 30\n", encoding="utf-8")
        self.enterContext(patch.object(settings_ext, "_YAML_PATH", str(self.config_path)))
        self.enterContext(patch("src.utils.debug_logger.log_secondary_llm_call"))
        self.enterContext(patch("src.utils.debug_logger.log_secondary_llm_response"))
        self.enterContext(patch("src.memory.memory_manager.log_summary_generate"))
        self.enterContext(patch("src.memory.memory_manager.log_memory_startup"))
        self.manager = MemoryManager("fixture", str(self.root),
            app_memory_cfg=self.config.get_memory_config(), profile_memory_cfg={
                "extraction_prompt": "STALE_PRIVATE_EXTRACT", "day_summary_prompt": "STALE_PRIVATE_DIARY",
            })
        self.messages = []

        async def stream_chat(messages):
            self.messages.append(messages)
            yield "[]" if len(self.messages) % 2 else "Synthetic diary."

        self.enterContext(patch("src.llm.registry.get_provider", return_value=SimpleNamespace(stream_chat=stream_chat)))

    async def invoke_both(self, profile):
        ts = datetime(2026, 1, 15, 12).timestamp()
        rows = [{"id": 101, "role": "user", "content": "A fixture utterance.", "timestamp": ts}]
        await self.manager._extract_facts_llm(rows, profile, self.app)
        await self.manager.generate_day_summary("2026-01-15", None, profile, self.app, force=True,
            day_msgs_override=[Message("user", "A fixture utterance.", ts)])

    async def test_legacy_card_macros_are_expanded_in_both_memory_requests(self):
        profile = {"display_name": "Fixture", "base_prompt": "<CHAR> talks to <USER>."}
        await self.invoke_both(profile)
        for messages in self.messages:
            self.assertIn("Fixture talks to Human.", messages[0]["content"])

    async def test_backfill_keeps_only_prior_diaries_and_preserves_speakers(self):
        self.manager.day_store.add_summary("2026-01-14", "PRIOR_ENTRY")
        self.manager.day_store.add_summary("2026-01-16", "LATER_ENTRY")
        ts = datetime(2026, 1, 15, 12).timestamp()
        rows = [Message("assistant", "First utterance", ts, "Speaker One"),
                Message("assistant", "Second utterance", ts + 1, "Speaker Two")]
        await self.manager.generate_day_summary("2026-01-15", None, {"display_name": "Diary Author"},
                                                 self.app, force=True, day_msgs_override=rows)
        context = self.messages[-1][1]["content"]
        self.assertIn("Diary Author", context)
        self.assertIn("PRIOR_ENTRY", context)
        self.assertNotIn("LATER_ENTRY", context)
        self.assertIn("Speaker One: First utterance", context)
        self.assertIn("Speaker Two: Second utterance", context)

    async def test_wizard_does_not_send_or_accept_legacy_memory_prompt_fields(self):
        retired = {"memory_extraction_prompt": "OLD_EXTRACT", "memory_day_summary_prompt": "OLD_DIARY"}
        calls = []
        async def stream_chat(messages):
            calls.append(messages)
            yield json.dumps({"display_name": "Fixture", "style_constraint": "Fixture style", **retired})

        with patch("src.llm.registry.get_provider", return_value=SimpleNamespace(stream_chat=stream_chat)):
            async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://localhost") as client:
                generated = await client.post("/profiles/wizard", json={"description": "A synthetic character."})
                self.assertTrue(generated.json()["ok"])
                self.assertTrue(retired.keys().isdisjoint(generated.json()["profile"]))
                filled = await client.post("/profiles/prompt_autofill", json={
                    "snapshot": {"base_prompt": "A synthetic character for testing.", **retired},
                    "scopes": ["style"],
                })
                self.assertTrue(filled.json()["ok"])
                self.assertTrue(retired.keys().isdisjoint(filled.json()["patch"]))
                for messages in calls:
                    text = "\n".join(message["content"] for message in messages)
                    for key, value in retired.items():
                        self.assertNotIn(key, text)
                        self.assertNotIn(value, text)

    async def test_shared_defaults_hot_reload_and_persona_resolution(self):
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://localhost") as client:
            original = (await client.get("/settings/memory/prompts")).json()
            self.assertTrue(original["defaults"]["extraction_prompt"])
            self.assertTrue(original["defaults"]["day_summary_prompt"])
            self.assertEqual(original["overrides"]["extraction_prompt"], None)
            edited = await client.patch("/settings/memory/prompts", json={
                "extraction_prompt": "GLOBAL FACT TASK", "day_summary_prompt": "GLOBAL DIARY TASK",
            })
            self.assertEqual(edited.status_code, 200, edited.text)

            profiles = [
                {"display_name": "One", "base_prompt": "OLD SOURCE",
                 "style_constraint": "BASE STYLE", "persona_evolved": {
                     "enabled": True, "base_prompt": "Long evolved profile " + "x" * 3000 + " END OF PERSONA",
                     "style_constraint": "ACTIVE STYLE",
                 }, "reflection_config": {"custom_prompt": "UNRELATED REFLECTION"}},
                {"display_name": "Two", "base_prompt": "SECOND SOURCE", "style_constraint": "SECOND STYLE",
                 "persona_evolved": {"enabled": False, "base_prompt": "DISABLED EVOLUTION"}},
            ]
            for profile in profiles:
                await self.invoke_both(profile)
                chat = PersonaSegment().build(SimpleNamespace(profile=profile)).messages[0]["content"]
                # The final chat-only agency block has a heading and a paragraph separated by one newline.
                persona_text = chat.rsplit("\n\n", 1)[0]
                for messages, task in zip(self.messages[-2:], ("GLOBAL FACT TASK", "GLOBAL DIARY TASK")):
                    self.assertEqual(messages[0]["role"], "system")
                    self.assertIn(persona_text, messages[0]["content"])
                    self.assertIn(task, messages[0]["content"])
                    self.assertNotIn("STALE_PRIVATE", messages[0]["content"])
                    self.assertNotIn("UNRELATED REFLECTION", messages[0]["content"])
                    self.assertNotIn("DISABLED EVOLUTION", messages[0]["content"])
                    self.assertIn("A fixture utterance.", messages[1]["content"])

            reset = await client.patch("/settings/memory/prompts", json={"extraction_prompt": None})
            self.assertEqual(reset.status_code, 200, reset.text)
            self.assertEqual(reset.json()["overrides"], {"extraction_prompt": None, "day_summary_prompt": "GLOBAL DIARY TASK"})
            await self.invoke_both(profiles[1])
            self.assertIn(original["defaults"]["extraction_prompt"], self.messages[-2][0]["content"])
            self.assertNotIn("GLOBAL FACT TASK", self.messages[-2][0]["content"])
            self.assertIn("GLOBAL DIARY TASK", self.messages[-1][0]["content"])
            saved = self.config_path.read_text(encoding="utf-8")
            self.assertIn("extraction_frequency: 30", saved)
            self.assertIn("GLOBAL DIARY TASK", saved)
            self.assertNotIn("GLOBAL FACT TASK", saved)
