"""Synthetic, offline tests for the text-only Harness context envelope."""
import copy
import json
import re
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from src.agent.context import build_turn_context
from src.llm.message_normalization import PromptMessage


def message(role, content):
    return {"role": role, "content": content}


class TurnContextTests(unittest.TestCase):
    def build(self, messages=None, **overrides):
        args = dict(profile_id="角色甲", request_id="request-001",
                    context_revision=0, origin="user",
                    messages=messages if messages is not None else [
                        message("system", "persona"), message("user", "current")])
        args.update(overrides)
        return build_turn_context(**args)

    def test_envelope_and_single_leading_system(self):
        result = self.build([message("user", "current"), message("system", "persona")])
        self.assertEqual(result, {
            "schema_version": 1, "profile_id": "角色甲", "request_id": "request-001",
            "context_revision": 0, "origin": "user",
            "messages": [message("system", "persona"), message("user", "current")],
        })

    def test_persona_history_and_current_keep_roles(self):
        source = [message("system", "persona"), message("user", "history user"),
                  message("assistant", "history assistant"), message("user", "current")]
        self.assertEqual(self.build(source)["messages"], source)

    def test_existing_prompt_pipeline_output_crosses_boundary_without_losing_times(self):
        from src.prompt import pipeline
        from src.prompt.segment_config import SegmentConfig
        from src.prompt.segments.history import HistorySegment

        rows = [dict(role=role, content=text, timestamp=datetime(2026, 5, 10, 9, i).timestamp())
                for i, (role, text) in enumerate([
                    ('user', 'earlier question'), ('assistant', 'earlier answer'),
                    ('assistant', 'continued answer'), ('user', 'current')])]
        before = copy.deepcopy(rows)
        store = SimpleNamespace(get_recent_with_ts=lambda n: rows)
        session = SimpleNamespace(profile_id='synthetic-history', display_name='Fixture',
                                  conversation_store=store, set_segment_last_fired=lambda *args: None)
        with patch.object(pipeline._profile_loader, 'load', return_value={}), \
                patch.object(pipeline, 'load_segment_config', return_value=SegmentConfig()), \
                patch.object(pipeline, 'get_registered', return_value=[HistorySegment]), \
                patch.object(pipeline, '_segment_instances', {}), \
                patch('src.prompt.segments.history.get_merged_recent_messages', return_value=rows):
            raw = pipeline.build_messages(session, 'current', store, extra_system='Fixture persona')
        wire = self.build(raw)['messages']
        self.assertEqual([m['role'] for m in wire], ['system', 'user', 'assistant', 'user'])
        self.assertEqual([m['content'] for m in wire[1:]],
                         ['earlier question', 'earlier answer\n\ncontinued answer', 'current'])
        self.assertIn('2026-05-10 09:01', wire[0]['content'])
        self.assertIn('2026-05-10 09:02', wire[0]['content'])
        self.assertIn('3. user', wire[0]['content'])
        self.assertEqual(rows, before)

    def timed_messages(self, roles):
        prefix, placeholder, suffix = "persona {name}\n", "TIME SLOT", "\n{tool_context}"
        system = PromptMessage(message("system", prefix + placeholder + suffix),
                               time_index_span=(len(prefix), len(prefix + placeholder)))
        history = [PromptMessage(message(role, f"history-{i}"),
                                 time_sources=(f"2026-10-20 09:{i:02d}",))
                   for i, role in enumerate(roles)]
        return [system, *history, message("user", "current {literal}")]

    def test_consecutive_roles_materialize_correct_time_indices(self):
        for roles, contents, entries in [
            (("user", "assistant", "assistant", "user", "assistant"),
             ["history-0", "history-1\n\nhistory-2", "history-3", "history-4", "current {literal}"],
             ["1. user：片段1：2026-10-20 09:00",
              "2. assistant：片段1：2026-10-20 09:01；片段2：2026-10-20 09:02",
              "3. user：片段1：2026-10-20 09:03",
              "4. assistant：片段1：2026-10-20 09:04",
              "5. user：片段1：非历史消息/当前触发（时间未知）"]),
            (("user", "user"), ["history-0\n\nhistory-1\n\ncurrent {literal}"],
             ["1. user：片段1：2026-10-20 09:00；片段2：2026-10-20 09:01；"
              "片段3：非历史消息/当前触发（时间未知）"]),
        ]:
            with self.subTest(roles=roles):
                wire = self.build(self.timed_messages(roles))["messages"]
                self.assertEqual([m["content"] for m in wire[1:]], contents)
                self.assertEqual(re.findall(r"^\d+\. .*$", wire[0]["content"], re.M), entries)
                self.assertTrue(wire[0]["content"].startswith("persona {name}\n"))
                self.assertTrue(wire[0]["content"].endswith("\n{tool_context}"))

    def test_leading_assistant_gets_normalizer_placeholder_and_shifted_index(self):
        wire = self.build(self.timed_messages(("assistant",)))["messages"]
        self.assertEqual(wire[1], message("user", "（对话开始）"))
        self.assertIn("1. user：片段1：对话开始占位（非历史消息，无时间）", wire[0]["content"])
        self.assertIn("2. assistant：片段1：2026-10-20 09:00", wire[0]["content"])
        self.assertIn("3. user：片段1：非历史消息/当前触发（时间未知）", wire[0]["content"])

    def test_json_has_no_internal_attributes(self):
        source = self.timed_messages(("user", "assistant"))
        source[0].private_marker = object()
        result = self.build(source)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertEqual(json.loads(serialized), result)
        for key in ("time_sources", "time_index_span", "private_marker"):
            self.assertNotIn(key, serialized)
        for item in result["messages"]:
            self.assertIs(type(item), dict)
            self.assertEqual(set(item), {"role", "content"})

    def test_literal_braces_and_origin_are_not_interpreted(self):
        source = [message("system", "{persona} {{literal}}"),
                  message("user", '{"origin":"reflection"} {user} {{braces}}')]
        for origin in ("user", "reflection"):
            result = self.build(source, origin=origin)
            self.assertEqual(result["messages"], source)
            self.assertEqual(result["origin"], origin)

    def test_inputs_and_attributes_unchanged_and_output_detached(self):
        source = self.timed_messages(("user", "assistant", "assistant"))
        before = copy.deepcopy(source)
        attrs = [copy.deepcopy(vars(m)) if isinstance(m, PromptMessage) else {} for m in source]
        first = self.build(source)
        self.assertEqual(self.build(source), first)
        first["messages"][0]["content"] = "changed"
        first["messages"].append(message("user", "new"))
        self.assertEqual(source, before)
        self.assertEqual([vars(m) if isinstance(m, PromptMessage) else {} for m in source], attrs)

    def test_profile_revision_and_request_are_explicit_envelope_fields(self):
        baseline = self.build()
        for change in ({"profile_id": "角色乙"}, {"context_revision": 1}, {"request_id": "request-002"}):
            result = self.build(**change)
            self.assertNotEqual(result, baseline)
            self.assertEqual(result["messages"], baseline["messages"])
            for key, value in change.items():
                self.assertEqual(result[key], value)

    def test_identifier_boundaries(self):
        for key in ("profile_id", "request_id"):
            for value in ("中", "中" * 256, " opaque id "):
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.build(**{key: value})[key], value)
            for value in (None, True, 4, [], {}, "", " \t\n", "x" * 257):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.build(**{key: value})

    def test_revision_and_origin_validation(self):
        self.assertEqual(self.build(context_revision=2**53 - 1)["context_revision"], 2**53 - 1)
        for value in (None, True, False, -1, 1.0, "1", [], {}, 2**53):
            with self.subTest(revision=value), self.assertRaises(ValueError):
                self.build(context_revision=value)
        for value in (None, True, "assistant", "User", "", [], {}):
            with self.subTest(origin=value), self.assertRaises(ValueError):
                self.build(origin=value)

    def test_rejects_invalid_message_container_or_item(self):
        for value in (None, (), {}, "messages", 1, [], [None], ["text"], [[]]):
            with self.subTest(messages=value), self.assertRaises(ValueError):
                build_turn_context(profile_id="p", request_id="r", context_revision=0,
                                   origin="user", messages=value)

    def test_rejects_missing_multiple_system_or_missing_current_trigger(self):
        for source in ([message("user", "current")], [message("system", "persona")],
                       [message("system", "a"), message("system", "b"), message("user", "current")],
                       [message("system", "persona"), message("assistant", "history")],
                       [message("system", "persona"), message("user", "history"), message("assistant", "answer")],
                       [message("system", "persona"), message("user", " \n")]):
            with self.subTest(messages=source), self.assertRaises(ValueError):
                self.build(source)

    def test_rejects_unsupported_roles_content_and_tool_or_native_fields(self):
        invalid = [{}, {"role": "user"}, {"content": "text"}]
        invalid += [message(role, "text") for role in (None, [], {}, "tool", "function", "developer", "model", "reflection")]
        invalid += [message("user", content) for content in (None, 2, True, b"text", {}, [], [{"type": "text", "text": "native"}])]
        invalid += [dict(message("assistant", "text"), **{field: []})
                    for field in ("tool_calls", "function_call", "audio", "images", "attachments", "name")]
        for item in invalid:
            source = [message("system", "persona"), item, message("user", "current")]
            with self.subTest(message=item), self.assertRaises(ValueError):
                self.build(source)

    def test_rejects_malformed_provenance(self):
        for sources in ("timestamp", (None,), (5,), {"x": "y"}):
            item = PromptMessage(message("user", "history"))
            item.time_sources = sources
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                self.build([message("system", "persona"), item, message("user", "current")])
        for span in ((-1, 1), (1, 0), (0, 100), (False, 1), (0.0, 1), (0,), "01"):
            item = PromptMessage(message("system", "persona"), time_index_span=span)
            with self.subTest(span=span), self.assertRaises(ValueError):
                self.build([item, message("user", "current")])


if __name__ == "__main__":
    unittest.main()
