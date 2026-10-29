"""Offline regression tests for timestamps against the actual model message list."""
import asyncio
import copy
import json
import re
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.llm.openai_compat import OpenAICompatProvider, _normalize_messages
from src.prompt import pipeline
from src.prompt.base import PromptSegment, SegmentResult
from src.prompt.segment_config import SegmentConfig, SegmentMeta, CustomSegmentDef
from src.prompt.segments.history import HistorySegment


def history(*roles):
    return [dict(role=role, content=f"text-{i}",
                 timestamp=datetime(2026, 10, 20, 9, i).timestamp())
            for i, role in enumerate(roles)]


class HistoryTimeIndexTests(unittest.TestCase):
    def setUp(self):
        # Block real client construction; provider tests use an in-memory fake.
        guard = patch("src.llm.openai_compat.AsyncOpenAI", side_effect=AssertionError("offline test"))
        guard.start()
        self.addCleanup(guard.stop)

    def build(self, rows, current="current", config=None, segments=None, **kwargs):
        original = copy.deepcopy(rows)
        store = SimpleNamespace(get_recent_with_ts=lambda n: rows)
        session = SimpleNamespace(profile_id="offline-history-test", display_name="Alice",
                                  conversation_store=store,
                                  set_segment_last_fired=lambda *args: None)
        with patch.object(pipeline._profile_loader, "load", return_value={}), \
                patch.object(pipeline, "load_segment_config", return_value=config or SegmentConfig()), \
                patch.object(pipeline, "get_registered", return_value=segments or [HistorySegment]), \
                patch.object(pipeline, "_segment_instances", {}), \
                patch("src.prompt.segments.history.get_merged_recent_messages", return_value=rows):
            messages = pipeline.build_messages(session, current, store, **kwargs)
        self.assertEqual(rows, original, "source/database records must not be mutated")
        return messages

    def index(self, messages):
        systems = [m["content"] for m in messages if m["role"] == "system"]
        self.assertEqual(len(systems), 1)
        return dict((int(n), value) for n, value in
                    re.findall(r"^(\d+)\. (.*)$", systems[0], re.MULTILINE))

    def assert_wire(self, messages, contents):
        wire = _normalize_messages(messages)
        self.assertEqual([m["content"] for m in wire if m["role"] != "system"], contents)
        entries = self.index(wire)
        self.assertEqual(list(entries), list(range(1, len(contents) + 1)),
                         "time index must number the actual non-system messages")
        self.assertEqual(_normalize_messages(wire), wire, "wire normalization must be idempotent")
        for m in wire:
            self.assertEqual(set(m), {"role", "content"})
        roles = [m["role"] for m in wire if m["role"] != "system"]
        self.assertEqual(roles, ["user" if i % 2 == 0 else "assistant"
                                 for i in range(len(roles))])
        return entries

    def test_consecutive_assistants_keep_both_times_and_later_indices(self):
        messages = self.build(history("user", "assistant", "assistant", "user", "assistant"))
        self.assertIn("2026-10-20 09:03", self.index(_normalize_messages(messages))[3],
                      "actual message #3 is text-3, not the merged assistant fragment text-2")
        entries = self.assert_wire(messages, ["text-0", "text-1\n\ntext-2", "text-3", "text-4", "current"])
        self.assertIn("2026-10-20 09:01", entries[2])
        self.assertIn("2026-10-20 09:02", entries[2])
        self.assertIn("2026-10-20 09:03", entries[3])
        self.assertIn("2026-10-20 09:04", entries[4])
        self.assertIn("当前请求", entries[5])

    def test_group_chat_consecutive_users_keep_each_source_time(self):
        rows = history("user", "assistant", "assistant", "assistant")
        for row, sender in zip(rows, ("Human", "Bob", "Carol", "Alice")):
            row["sender"] = sender
        entries = self.assert_wire(self.build(rows, group_reply_as_sender="Alice"),
                                   ["text-0\n\n[Bob]: text-1\n\n[Carol]: text-2", "text-3", "current"])
        for minute in range(3):
            self.assertIn(f"2026-10-20 09:0{minute}", entries[1])
        self.assertIn("2026-10-20 09:03", entries[2])

    def test_merged_group_timeline_uses_source_timestamps(self):
        rows = history("user", "assistant", "assistant")
        for row, sender in zip(rows, ("Human", "Bob", "Alice")):
            row.update(source="group", gname="Room", sender=sender)
        app = SimpleNamespace(state=SimpleNamespace(group_manager=object()))
        entries = self.assert_wire(self.build(rows, app=app, group_reply_as_sender="Alice",
                                               current_group_gname="Room"),
                                   ["text-0\n\n[Bob]: text-1", "text-2", "current"])
        self.assertIn("2026-10-20 09:00", entries[1])
        self.assertIn("2026-10-20 09:01", entries[1])
        self.assertIn("2026-10-20 09:02", entries[2])

    def test_leading_assistant_placeholder_has_no_historical_time(self):
        entries = self.assert_wire(self.build(history("assistant")),
                                   ["（对话开始）", "text-0", "current"])
        self.assertIn("占位", entries[1])
        self.assertNotIn("2026-", entries[1])
        self.assertIn("2026-10-20 09:00", entries[2])

    def test_historical_last_user_and_current_user_are_distinguished(self):
        entries = self.assert_wire(self.build(history("user")), ["text-0\n\ncurrent"])
        self.assertIn("片段1", entries[1])
        self.assertIn("2026-10-20 09:00", entries[1])
        self.assertIn("片段2", entries[1])
        self.assertIn("当前请求", entries[1])

    def test_current_user_already_in_store_is_not_assigned_old_time(self):
        rows = history("user", "assistant", "user")
        entries = self.assert_wire(self.build(rows, current="text-2"), ["text-0", "text-1", "text-2"])
        self.assertIn("当前请求", entries[3])
        self.assertNotIn("2026-10-20 09:02", entries[3])

    def test_unknown_timestamp_and_multiline_body_are_not_parsed(self):
        rows = history("user", "assistant")
        rows[0].update(timestamp=None, content="【群「2099-01-01」】Bob: first\n\nsecond")
        entries = self.assert_wire(self.build(rows), [rows[0]["content"], "text-1", "current"])
        self.assertIn("时间未知", entries[1])
        self.assertNotIn("2099", entries[1])

    def test_prefix_suffix_macros_and_system_priority_survive(self):
        class Prefix(PromptSegment):
            segment_id, priority = "test-prefix", 10
            def build(self, ctx):
                return SegmentResult(messages=[{"role": "user", "content": "prefix {{char}}"}])
        class Suffix(PromptSegment):
            segment_id, priority = "test-suffix", 100
            def build(self, ctx):
                return SegmentResult(messages=[{"role": "assistant", "content": "suffix {{char}}"}])
        config = SegmentConfig(custom_segments=[
            CustomSegmentDef("before", "before", "BEFORE {{char}}", priority=80),
            CustomSegmentDef("after", "after", "AFTER {{char}}", priority=100),
        ])
        rows = history("assistant", "user")
        rows[0]["content"] = "hello {{char}}"
        messages = self.build(rows, config=config, segments=[Suffix, HistorySegment, Prefix])
        entries = self.assert_wire(messages, ["prefix Alice", "hello Alice", "text-1", "suffix Alice", "current"])
        self.assertNotIn("2026-", entries[1])
        self.assertIn("2026-10-20 09:00", entries[2])
        self.assertIn("2026-10-20 09:01", entries[3])
        system = messages[0]["content"]
        self.assertLess(system.index("BEFORE Alice"), system.index("【历史消息时间索引】"))
        self.assertLess(system.index("【历史消息时间索引】"), system.index("AFTER Alice"))

    def test_index_respects_history_priority_override(self):
        config = SegmentConfig(overrides={"history": SegmentMeta("history", priority=95)}, custom_segments=[
            CustomSegmentDef("before", "before", "BEFORE", priority=94),
            CustomSegmentDef("after", "after", "AFTER", priority=96),
        ])
        messages = self.build(history("user", "assistant"), config=config)
        self.assert_wire(messages, ["text-0", "text-1", "current"])
        system = messages[0]["content"]
        self.assertLess(system.index("BEFORE"), system.index("【历史消息时间索引】"))
        self.assertLess(system.index("【历史消息时间索引】"), system.index("AFTER"))

    def test_override_is_current_even_when_merged_with_history(self):
        override = {"role": "user", "content": "override {{char}}"}
        entries = self.assert_wire(self.build(history("user"), last_message_override=override),
                                   ["text-0\n\noverride Alice"])
        self.assertIn("当前请求", entries[1])

    def test_empty_history_has_no_fabricated_time_index(self):
        messages = self.build([], extra_system="persona {{char}}")
        self.assertEqual(messages, [{"role": "system", "content": "persona Alice"},
                                    {"role": "user", "content": "current"}])

    def test_preview_matches_wire_and_private_sources_are_not_serialized(self):
        messages = self.build(history("assistant", "assistant", "user"))
        original = copy.deepcopy(messages)
        wire = _normalize_messages(messages)
        self.assertEqual(messages, original)
        self.assertEqual(messages[0].time_index_span, original[0].time_index_span)
        self.assertEqual(messages[1].time_sources, original[1].time_sources)
        self.assertEqual(wire, _normalize_messages(messages))
        self.assertEqual(messages[0]["content"], wire[0]["content"])
        for message in json.loads(json.dumps(messages)):
            self.assertEqual(set(message), {"role", "content"})
        self.assertTrue(all(type(message) is dict for message in wire))

    def test_post_build_append_trigger_after_last_user(self):
        messages = self.build(history("user"))
        messages.append({"role": "user", "content": "trigger"})
        entries = self.assert_wire(messages, ["text-0\n\ncurrent\n\ntrigger"])
        self.assertIn("2026-10-20 09:00", entries[1])
        self.assertIn("片段2", entries[1])
        self.assertIn("当前请求", entries[1])
        self.assertIn("片段3", entries[1])
        self.assertIn("触发", entries[1])

    def test_timer_and_system_trigger_pop_empty_user_then_append(self):
        for roles in (("user",), ("assistant", "assistant")):
            with self.subTest(roles=roles):
                messages = self.build(history(*roles), current="")
                self.assertEqual(messages[-1]["content"], "", "callers must still be able to pop the empty turn")
                messages.pop()
                messages.append({"role": "user", "content": "timer trigger"})
                wire = _normalize_messages(messages)
                entries = self.index(wire)
                self.assertEqual(len(entries), len(wire) - 1)
                self.assertIn("触发", entries[len(entries)])
                self.assertNotIn("当前请求（", entries[len(entries)])
                if roles == ("user",):
                    self.assertEqual(wire[-1]["content"], "text-0\n\ntimer trigger")
                    self.assertIn("2026-10-20 09:00", entries[1])

    def test_post_build_replace_current_preserves_historical_last_user(self):
        messages = self.build(history("user"))
        messages[-1] = {"role": "user", "content": "replacement trigger"}
        entries = self.assert_wire(messages, ["text-0\n\nreplacement trigger"])
        self.assertIn("2026-10-20 09:00", entries[1])
        self.assertIn("触发", entries[1])

    def test_post_build_system_context_is_preserved(self):
        messages = self.build(history("user", "assistant"))
        # src.api.chat._inject_context appends tool/command context this way.
        messages[0]["content"] += "\n\nTOOL RESULT: retain this context"
        messages.append({"role": "user", "content": "trigger"})
        wire = _normalize_messages(messages)
        self.assertTrue(wire[0]["content"].endswith("TOOL RESULT: retain this context"))
        self.assertIn("触发", self.index(wire)[3])

    def test_provider_chat_and_stream_send_only_public_message_fields(self):
        messages = self.build(history("assistant", "assistant", "user"))
        messages.append({"role": "user", "content": "post-build trigger"})
        messages[0]["content"] += "\n\nTOOL RESULT"
        async def check():
            provider = object.__new__(OpenAICompatProvider)
            provider.model, provider._gen_kwargs, provider._extra_body = "offline", {}, None
            create = AsyncMock(return_value=SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content="ok"))]))
            provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            self.assertEqual(await provider.chat(messages), "ok")
            async def chunks():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok"))])
            create.return_value = chunks()
            self.assertEqual([part async for part in provider.stream_chat(messages)], ["ok"])
            for call in create.call_args_list:
                self.assertEqual(call.kwargs["messages"], _normalize_messages(messages))
                self.assertTrue(all(type(m) is dict for m in call.kwargs["messages"]))
                self.assertTrue(all(set(m) == {"role", "content"} for m in call.kwargs["messages"]))
                self.assertEqual(call.kwargs["messages"][-1]["content"], "text-2\n\ncurrent\n\npost-build trigger")
                self.assertIn("触发", self.index(call.kwargs["messages"])[3])
                self.assertTrue(call.kwargs["messages"][0]["content"].endswith("TOOL RESULT"))
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
