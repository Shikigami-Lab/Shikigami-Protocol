import json
import unittest

from src.commands.nl_triggers import TopicNLTrigger
from src.llm.think_filter import ThinkTagFilter, strip_think_tags
from src.prompt.macros import build_macro_table, expand_macros, expand_messages


class TopicTriggerTests(unittest.TestCase):
    def setUp(self):
        self.trigger = TopicNLTrigger()

    def test_bare_requests_fire(self):
        for msg in [
            "换个话题", "换个话题吧", "那我们换个话题吧", "咱们聊点别的呗", "有什么新鲜事吗",
            "最近有什么新鲜事吗", "聊点新鲜的好不好", "anything new?", "hey, anything new?",
            "Let's talk about something else", "is there anything new?",
        ]:
            with self.subTest(msg=msg):
                self.assertTrue(self.trigger.detect(msg))

    def test_negations_statements_and_pointed_requests_do_not_fire(self):
        for msg in [
            "别换个话题", "不要换个话题", "不想换个话题", "先别换个话题", "又换个话题", "你又换个话题",
            "别说点别的", "不聊点别的", "干嘛换个话题", "没有什么新鲜事", "我没有什么新鲜事",
            "还没有什么新鲜事", "也没有什么新鲜事啊", "学校有什么新鲜事", "公司有什么新鲜事吗",
            "换个话题，聊聊电影", "查查有什么新鲜事", "今天好累",
        ]:
            with self.subTest(msg=msg):
                self.assertFalse(self.trigger.detect(msg))


class LlmJsonExtractionTests(unittest.TestCase):
    def setUp(self):
        from src.api.settings_ext import _prepare_llm_json
        self.prepare = _prepare_llm_json

    def _parse(self, reply):
        raw = self.prepare(reply)
        self.assertIsInstance(raw, str)
        return json.loads(raw, strict=False)  # same as the wizard and autofill callers

    def test_two_fenced_blocks_use_the_first_object(self):
        self.assertEqual({"a": 1}, self._parse('```json\n{"a": 1}\n```\n\n```json\n{"b": 2}\n```'))

    def test_raw_newlines_inside_strings(self):
        self.assertEqual("line1\nline2", self._parse('{"base_prompt": "line1\nline2"}')["base_prompt"])

    def test_plain_and_fenced(self):
        self.assertEqual({"a": 1}, self._parse('{"a": 1}'))
        self.assertEqual({"a": 1}, self._parse('```json\n{"a": 1}\n```'))

    def test_prose_around_fence(self):
        self.assertEqual({"a": 1}, self._parse('Here it is:\n```json\n{"a": 1}\n```'))
        self.assertEqual({"a": 1}, self._parse('```json\n{"a": 1}\n```\nLet me know if you need changes.'))

    def test_fence_inside_json_string(self):
        reply = '```json\n{"code": "```py\\nprint(1)\\n```", "b": 2}\n```'
        self.assertEqual(2, self._parse(reply)["b"])

    def test_prose_without_fence(self):
        self.assertEqual({"a": 1}, self._parse('Sure! {"a": 1} Hope this helps.'))

    def test_empty_and_llm_error_are_reported(self):
        self.assertIsInstance(self.prepare(""), dict)
        self.assertIsInstance(self.prepare("[LLM Error: timeout]"), dict)


class MacroTests(unittest.TestCase):
    def test_legacy_tags_only_expand_in_system_messages(self):
        table = build_macro_table(char_name="Mochi", user_name="Alex")
        messages = [
            {"role": "system", "content": "<CHAR> talks to <USER>. {{char}} likes {{ user }}."},
            {"role": "user", "content": "std::vector<char> v; <user>name</user> {{char}}"},
        ]
        expand_messages(messages, table)
        self.assertEqual("Mochi talks to Alex. Mochi likes Alex.", messages[0]["content"])
        self.assertEqual("std::vector<char> v; <user>name</user> Mochi", messages[1]["content"])

    def test_chat_messages_only_expand_names(self):
        table = build_macro_table(char_name="Mochi", user_name="Alex")
        messages = [
            {"role": "system", "content": "Search result: std::vector<char> v;"},
            {"role": "user", "content": "<p>Hello {{ user }}</p> {{date}} {{description}} {{trim}}"},
        ]
        expand_messages(messages, table)
        self.assertEqual("Search result: std::vector<char> v;", messages[0]["content"])
        self.assertEqual("<p>Hello Alex</p> {{date}} {{description}} {{trim}}", messages[1]["content"])

    def test_unknown_name_leaves_macro_literal(self):
        table = build_macro_table(char_name="", user_name="Alex")
        self.assertEqual("{{char}} and Alex", expand_macros("{{char}} and {{user}}", table))


class ThinkFilterTests(unittest.TestCase):
    def _stream(self, chunks):
        flt = ThinkTagFilter()
        return "".join(flt.feed(c) for c in chunks) + flt.flush()

    def test_leading_block_removed_across_chunk_boundaries(self):
        text = "<think>plan the reply</think>\n\nHello there"
        self.assertEqual("Hello there", strip_think_tags(text))
        for size in (1, 2, 3, 5, 8):
            chunks = [text[i:i + size] for i in range(0, len(text), size)]
            with self.subTest(size=size):
                self.assertEqual("Hello there", self._stream(chunks))

    def test_literal_tag_after_visible_text_is_kept(self):
        text = "You can wrap reasoning in <think> tags like this."
        self.assertEqual(text, strip_think_tags(text))
        self.assertEqual(text, self._stream([text[i:i + 4] for i in range(0, len(text), 4)]))

    def test_uppercase_tags(self):
        self.assertEqual("Answer", strip_think_tags("<THINK>x</THINK>Answer"))

    def test_unclosed_leading_block_is_dropped(self):
        self.assertEqual("", strip_think_tags("<think>cut off by max_tokens"))

    def test_text_starting_with_angle_bracket(self):
        self.assertEqual("<3 you", self._stream(["<", "3 you"]))


if __name__ == "__main__":
    unittest.main()
