import json
import time
import unittest

from src.core.persona_evolution import (
    _is_baseline_review,
    _validate_evolution_output,
)
from src.core.user_portrait import _parse_portrait_payload, extract_style_address_form, format_mutual_address_facts
from src.prompt.segments.persona import _user_agency_text
from src.prompt.segments.user_persona import (
    _get_fresh_recent_context,
    _matching_contextual_details,
)


class PersonaEvolutionGuardTests(unittest.TestCase):
    def test_rejects_evolution_that_drops_original_section(self):
        ok, _, reason = _validate_evolution_output(
            original_base="【身份】\nA\n【性格】\nB",
            new_base="【身份】\nA changed",
            current_style="short",
            new_style="short",
        )

        self.assertFalse(ok)
        self.assertIn("【性格】", reason)

    def test_empty_new_style_keeps_current_style(self):
        ok, effective_style, reason = _validate_evolution_output(
            original_base="【身份】\nA",
            new_base="【身份】\nA changed",
            current_style="concise",
            new_style="",
        )

        self.assertTrue(ok, reason)
        self.assertEqual("concise", effective_style)

    def test_every_third_completed_evolution_is_baseline_review(self):
        self.assertFalse(_is_baseline_review(evolution_count=0, interval=3))
        self.assertFalse(_is_baseline_review(evolution_count=1, interval=3))
        self.assertTrue(_is_baseline_review(evolution_count=2, interval=3))
        self.assertFalse(_is_baseline_review(evolution_count=3, interval=3))
        self.assertFalse(_is_baseline_review(evolution_count=2, interval=0))


class UserPortraitLayerTests(unittest.TestCase):
    def test_parses_three_layer_portrait_payload(self):
        payload = {
            "content": "Stable profile",
            "recent_context": "Preparing for an interview",
            "contextual_details": [
                {
                    "summary": "Prefers window seats when travelling",
                    "keywords": ["flight", "seat"],
                }
            ],
        }

        parsed = _parse_portrait_payload(json.dumps(payload))

        self.assertEqual("Stable profile", parsed["content"])
        self.assertEqual("Preparing for an interview", parsed["recent_context"])
        self.assertEqual(
            [{"summary": "Prefers window seats when travelling", "keywords": ["flight", "seat"]}],
            parsed["contextual_details"],
        )

    def test_invalid_portrait_payload_returns_none(self):
        self.assertIsNone(_parse_portrait_payload("not-json"))

    def test_contextual_detail_requires_two_keywords(self):
        payload = {
            "content": "Stable profile",
            "recent_context": "",
            "contextual_details": [
                {"summary": "Too broad to recall safely", "keywords": ["topic"]}
            ],
        }

        parsed = _parse_portrait_payload(json.dumps(payload))

        self.assertEqual([], parsed["contextual_details"])

    def test_recent_context_expires_after_ttl(self):
        now = time.time()
        portrait = {
            "recent_context": {
                "content": "Short-lived context",
                "updated_at": now - 73 * 3600,
            }
        }

        self.assertEqual("", _get_fresh_recent_context(portrait, ttl_hours=72, now_ts=now))

    def test_contextual_detail_requires_keyword_match(self):
        portrait = {
            "contextual_details": [
                {
                    "summary": "Prefers window seats when travelling",
                    "keywords": ["flight", "seat"],
                },
                {
                    "summary": "Uses dark mode for coding",
                    "keywords": ["editor", "theme"],
                },
            ]
        }

        matched = _matching_contextual_details(portrait, "Which seat should I choose for my flight?")

        self.assertEqual(["Prefers window seats when travelling"], matched)


class MutualAddressTests(unittest.TestCase):
    def test_format_mutual_address_both_directions(self):
        text = format_mutual_address_facts(
            "主人",
            {"address_forms": {"forms": [{"form": "阿猫", "count": 12}]}},
            locale="zh",
        )
        self.assertIn("你称呼对方为「主人」", text)
        self.assertIn("对方称呼你为「阿猫」", text)

    def test_style_address_form_from_affirmative_rule(self):
        form = extract_style_address_form(
            {"style_constraint": "【称呼】称呼主人为「主人」，第二人称用「你」。"},
        )
        self.assertEqual("主人", form)


class UserAgencyTests(unittest.TestCase):
    def test_user_agency_guard_is_available_in_both_locales(self):
        zh = _user_agency_text("zh")
        en = _user_agency_text("en")

        self.assertIn("不要把对方未表达", zh)
        self.assertIn("Do not treat", en)
        self.assertNotEqual(zh, en)


if __name__ == "__main__":
    unittest.main()
