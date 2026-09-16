"""Lenient parsing of portrait and address-form replies from the model."""
import json
import unittest

from src.core.user_portrait import (
    _parse_address_forms,
    _parse_address_forms_or_none,
    _parse_portrait_payload,
)

CONTENT = "第一段：周末常去图书馆。\n\n第二段：说过做事前习惯先列清单。"


def _portrait_json(raw_newlines=False):
    text = json.dumps(
        {
            "content": CONTENT,
            "recent_context": "这周在准备搬家",
            "contextual_details": [{"summary": "搬家定在月底", "keywords": ["搬家", "月底"]}],
        },
        ensure_ascii=False,
        indent=2,
    )
    if raw_newlines:
        # Models often emit real line breaks inside long string values.
        text = text.replace("\\n", "\n")
    return text


class PortraitPayloadParsingTests(unittest.TestCase):
    def assertParsed(self, raw):
        parsed = _parse_portrait_payload(raw)
        self.assertIsNotNone(parsed, raw[:80])
        self.assertEqual(CONTENT, parsed["content"])
        self.assertEqual("这周在准备搬家", parsed["recent_context"])
        self.assertEqual([{"summary": "搬家定在月底", "keywords": ["搬家", "月底"]}], parsed["contextual_details"])

    def test_fenced_reply(self):
        self.assertParsed("```json\n" + _portrait_json() + "\n```")
        self.assertParsed("```JSON\r\n" + _portrait_json() + "\r\n```")

    def test_prose_around_fence(self):
        self.assertParsed("好的，以下是更新后的画像：\n```json\n" + _portrait_json() + "\n```\n以上。")

    def test_raw_newlines_inside_strings(self):
        raw = _portrait_json(raw_newlines=True)
        with self.assertRaises(ValueError):
            json.loads(raw)
        self.assertParsed(raw)
        self.assertParsed("```json\n" + raw + "\n```")
        self.assertParsed("Here it is:\n```json\n" + raw + "\n```\nDone.")

    def test_prose_without_fence(self):
        self.assertParsed("结果如下\n" + _portrait_json() + "\n请查收")

    def test_code_fence_inside_content_is_kept(self):
        content = "他分享过一段脚本：\n```\nprint(1)\n```\n之后还提到别的。"
        raw = "```json\n" + json.dumps({"content": content}, ensure_ascii=False) + "\n```"
        self.assertEqual(content, _parse_portrait_payload(raw)["content"])

    def test_required_content_is_still_validated(self):
        self.assertIsNone(_parse_portrait_payload('```json\n{"recent_context": "x"}\n```'))
        self.assertIsNone(_parse_portrait_payload('{"content": "   "}'))
        self.assertIsNone(_parse_portrait_payload("没有 JSON"))
        self.assertIsNone(_parse_portrait_payload(""))

    def test_keywords_must_be_a_list(self):
        raw = json.dumps({
            "content": "x",
            "contextual_details": [{"summary": "s", "keywords": "flight"}],
        })
        self.assertEqual([], _parse_portrait_payload(raw)["contextual_details"])


class AddressFormParsingTests(unittest.TestCase):
    def test_array_followed_by_prose(self):
        forms = _parse_address_forms('[{"form": "小A", "count": 2, "evidence": "小A，在吗"}]\n以上')
        self.assertEqual([{"form": "小A", "count": 2, "evidence": "小A，在吗"}], forms)

    def test_prose_with_brackets_before_array(self):
        forms = _parse_address_forms('说明[1]：\n```json\n[{"form": "小A", "count": 1}]\n```')
        self.assertEqual(["小A"], [f["form"] for f in forms])

    def test_empty_array_versus_unreadable_reply(self):
        self.assertEqual([], _parse_address_forms_or_none("[]"))
        self.assertEqual([], _parse_address_forms_or_none("```json\n[]\n```\n没有找到"))
        self.assertIsNone(_parse_address_forms_or_none("没有找到称呼"))
        self.assertEqual([], _parse_address_forms("没有找到称呼"))


if __name__ == "__main__":
    unittest.main()
