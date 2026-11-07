import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.commands.topic_cmds import handle_topic
from src.config.app_config import AppConfig
from src.core.topics import TopicSourceContext, all_source_ids, get_enabled_sources, get_source


class TopicSourceTests(unittest.IsolatedAsyncioTestCase):
    def test_chat_pipeline_loads_weather_without_the_retired_trend_segment(self):
        from src.prompt.pipeline import get_registered

        segment_ids = {segment.segment_id for segment in get_registered()}
        self.assertIn("weather", segment_ids)
        self.assertNotIn("trend_awareness", segment_ids)

    def test_web_research_is_not_a_candidate_source(self):
        self.assertNotIn("trend", all_source_ids())
        self.assertNotIn("web_research", all_source_ids())
        enabled = get_enabled_sources({"sources": {"trend": {"enabled": True}, "web_research": {"enabled": True}}})
        self.assertFalse({"trend", "web_research"} & {source.source_id for source in enabled})

    async def test_manual_life_topic_honors_the_existing_source_setting(self):
        with tempfile.TemporaryDirectory() as root, patch("src.config.effective_config.PROFILES_DIR", root):
            session = SimpleNamespace(profile_id="example", storage_root=root)
            fact = "Synthetic fact for the manual command."
            memory = SimpleNamespace(get_reflection_context_facts=lambda **kwargs: [{"content": fact}])
            config = AppConfig()
            app = SimpleNamespace(state=SimpleNamespace(config=config, memory_managers={"example": memory}))
            self.assertIn(fact, (await handle_topic("life", "", session, app)).context_for_llm)
            config.topic_discovery = {"sources": {"user_life": {"enabled": False}}}
            self.assertNotIn(fact, (await handle_topic("life", "", session, app)).context_for_llm)

    def test_old_http_settings_and_cached_items_do_not_supply_random_topics(self):
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root) / "topic_random_cache.json"
            old_entry = {"text": "Externally acquired topic", "fetched_at": 1}
            cache.write_text(json.dumps({"items": {"old": old_entry}}), encoding="utf-8")
            ctx = TopicSourceContext("example", root, config={"sources": {"random_api": {
                "enabled": True, "mode": "http", "http_url": "https://old.invalid", "builtin_pool": "trivia",
            }}})
            source = get_source("random_api")
            self.assertIsNone(source.resolve("old", ctx))
            candidate, = source.get_candidates(ctx)
            material = source.resolve(candidate.item_id, ctx)
            self.assertIn("。", material.material)
            source.mark_used(candidate.item_id, ctx)
            next_candidate, = source.get_candidates(ctx)
            self.assertNotEqual(material.material, source.resolve(next_candidate.item_id, ctx).material)
            self.assertEqual(old_entry, json.loads(cache.read_text(encoding="utf-8"))["items"]["old"])
