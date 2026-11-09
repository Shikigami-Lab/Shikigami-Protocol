"""Offline tests for segment dependency locking (engine/tool/feature switches).

Covers the acceptance rules:
- a disabled engine/tool removes its segments from the prompt,
- the settings API can surface a human-readable reason for the lock,
- turning everything off leaves a valid prompt (persona + history + user),
- the tool guard notice fires only through the pipeline when tools are off.
"""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.prompt import pipeline
from src.prompt.base import PromptSegment, SegmentResult
from src.prompt.dependencies import unavailable_reason
from src.prompt.segment_config import SegmentConfig


class _FakeConfig:
    def __init__(self, engines=None, memory=None):
        self._engines = engines or {}
        self._memory = memory or {}

    def get_engine_config(self, name):
        return self._engines.get(name, {})

    def get_memory_config(self):
        return dict(self._memory)


def _app(engines=None, memory=None, runtime=None):
    return SimpleNamespace(state=SimpleNamespace(
        config=_FakeConfig(engines, memory),
        agent_runtime=runtime,
        user_name="用户",
        user_persona={},
    ))


class DependencyEvaluationTests(unittest.TestCase):
    def test_default_on_engine_disabled_only_explicitly(self):
        app = _app()
        self.assertIsNone(unavailable_reason(("engine:emotion",), app, "p1", {}))
        app_off = _app(engines={"emotion": {"enabled": False}})
        reason = unavailable_reason(("engine:emotion",), app_off, "p1", {})
        self.assertIn("情感引擎", reason)

    def test_default_off_engine_requires_explicit_enable(self):
        app = _app()
        self.assertIn("自省引擎", unavailable_reason(("engine:reflection",), app, "p1", {}))
        app_on = _app(engines={"reflection": {"enabled": True}})
        self.assertIsNone(unavailable_reason(("engine:reflection",), app_on, "p1", {}))

    def test_weather_defaults_off(self):
        app = _app()
        self.assertIn("天气", unavailable_reason(("tool:weather",), app, "p1", {}))
        profile = {"tool_configs": {"weather": {"enabled": True}}}
        self.assertIsNone(unavailable_reason(("tool:weather",), app, "p1", profile))

    def test_todo_defaults_on(self):
        app = _app()
        self.assertIsNone(unavailable_reason(("tool:todo",), app, "p1", {}))
        profile = {"tool_configs": {"todo": {"enabled": False}}}
        self.assertIn("待办", unavailable_reason(("tool:todo",), app, "p1", profile))

    def test_memory_master_switch(self):
        app = _app()
        self.assertIn("记忆", unavailable_reason(("feature:memory",), app, "p1", {}))
        app_on = _app(memory={"enabled": True})
        self.assertIsNone(unavailable_reason(("feature:memory",), app_on, "p1", {}))
        # Profile-level override wins over the global default.
        self.assertIsNone(unavailable_reason(("feature:memory",), app, "p1",
                                             {"memory_config": {"enabled": True}}))

    def test_agent_tools_missing_runtime(self):
        app = _app(runtime=None)
        self.assertIn("AI 工具", unavailable_reason(("feature:agent_tools",), app, "p1", {}))

    def test_missing_app_is_permissive(self):
        self.assertIsNone(unavailable_reason(("engine:emotion",), None, "p1", {}))

    def test_unknown_key_is_permissive(self):
        self.assertIsNone(unavailable_reason(("bogus:thing",), _app(), "p1", {}))


class _DependentSegment(PromptSegment):
    segment_id = "test_dependent_segment"
    priority = 50
    label = "依赖测试段"
    requires = ("engine:emotion",)

    def build(self, ctx):
        return SegmentResult(messages=[{"role": "system", "content": "DEPENDENT-CONTENT"}])


class _PlainSegment(PromptSegment):
    segment_id = "test_plain_segment"
    priority = 51
    label = "普通测试段"

    def build(self, ctx):
        return SegmentResult(messages=[{"role": "system", "content": "PLAIN-CONTENT"}])


def _session(store):
    return SimpleNamespace(
        profile_id="offline-deps-test",
        display_name="Alice",
        storage_root="",
        last_user_message_time=time.time(),
        conversation_store=store,
        get_segment_last_fired=lambda sid: 0.0,
        set_segment_last_fired=lambda *a: None,
        get_segment_last_fired_date=lambda sid: "",
        set_segment_last_fired_date=lambda *a: None,
    )


def _store():
    return SimpleNamespace(
        get_recent_with_ts=lambda n: [],
        get_recent=lambda n: [],
        total_user_messages_count=lambda u: 0,
    )


class PipelineDependencyTests(unittest.TestCase):
    def _build(self, segments, app, **kwargs):
        session = _session(_store())
        with patch.object(pipeline._profile_loader, "load", return_value={}), \
                patch.object(pipeline, "load_segment_config", return_value=SegmentConfig()), \
                patch.object(pipeline, "get_registered", return_value=segments), \
                patch.object(pipeline, "_segment_instances", {}):
            return pipeline.build_messages(session, "hello", session.conversation_store,
                                           app=app, **kwargs)

    def test_locked_segment_is_skipped(self):
        app = _app(engines={"emotion": {"enabled": False}})
        messages = self._build([_DependentSegment, _PlainSegment], app)
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        self.assertNotIn("DEPENDENT-CONTENT", system)
        self.assertIn("PLAIN-CONTENT", system)

    def test_available_segment_fires(self):
        messages = self._build([_DependentSegment], _app())
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        self.assertIn("DEPENDENT-CONTENT", system)


class FullRegistryHostileEnvironmentTests(unittest.TestCase):
    """All engines/memory/tools off + no agent runtime: build must not crash
    and must degrade to persona-only system content plus the user turn."""

    def test_build_with_everything_off(self):
        from src.prompt.registry import get_registered  # real registry
        import src.core.reflection  # noqa: F401 — register reflection segments too

        app = _app(
            engines={"emotion": {"enabled": False},
                     "affinity": {"enabled": False},
                     "energy": {"enabled": False}},
            memory={"enabled": False},
            runtime=None,
        )
        session = _session(_store())
        with patch.object(pipeline._profile_loader, "load", return_value={}), \
                patch.object(pipeline, "load_segment_config", return_value=SegmentConfig()), \
                patch.object(pipeline, "_segment_instances", {}), \
                patch("src.prompt.segments.history.get_merged_recent_messages", return_value=[]):
            messages = pipeline.build_messages(
                session, "hello", session.conversation_store, app=app,
                extra_context={"include_tool_capability": True,
                               "include_workspace_catalog": True})

        systems = [m["content"] for m in messages if m["role"] == "system"]
        self.assertEqual(len(systems), 1, "exactly one merged system message")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "hello")
        body = systems[0]
        # Engine/tool-gated content must be absent…
        self.assertNotIn("【待办事项】", body)
        self.assertNotIn("【进行中的计时器】", body)
        self.assertNotIn("【近期自主行动】", body)
        self.assertNotIn("Available capabilities", body)
        # …while the pipeline-owned tool guard notice is present (toggleable).
        self.assertIn("No tools are enabled", body)

    def test_core_segments_have_no_requires(self):
        import src.core.reflection  # noqa: F401
        from src.prompt.registry import get_registered

        for cls in get_registered():
            if cls.is_core:
                self.assertFalse(getattr(cls, "requires", ()),
                                 f"core segment {cls.segment_id} must not declare requires")


if __name__ == "__main__":
    unittest.main()
