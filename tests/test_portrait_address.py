"""The user's name and the character's form of address for the user stay separate.

Style rules are synthetic and modeled on the shipped example characters.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from src.core.user_portrait import extract_style_address_form, format_mutual_address_facts
from src.prompt.segments.user_persona import UserPersonaSegment

PRONOUN_RULE_STYLE = (
    "【风格约束】\n"
    "单次回复 60-120 字为宜。\n"
    "称呼用户为「你」，熟悉后可用对方的名字。\n"
)
FORM_RULE_STYLE = (
    "【风格约束】\n"
    "单次回复 20-80 字为宜。\n"
    "称呼用户为「投喂者」或直呼其名；极高信任后才自然改口。\n"
    "禁止写长篇道理。"
)


def _card(style, **extra):
    card = {"style_constraint": style}
    card.update(extra)
    return card


class StyleAddressFormTests(unittest.TestCase):
    def test_pronoun_is_never_a_form_of_address(self):
        self.assertEqual("", extract_style_address_form(_card(PRONOUN_RULE_STYLE)))
        self.assertEqual("", extract_style_address_form(_card('Call the user "you" by default, or by name once familiar.')))

    def test_form_of_address_is_extracted(self):
        self.assertEqual("投喂者", extract_style_address_form(_card(FORM_RULE_STYLE)))
        self.assertEqual("feeder", extract_style_address_form(_card('Call the user "feeder" or by name.')))

    def test_negated_rules_yield_nothing(self):
        for style in (
            "禁止称呼对方为「亲爱的」",
            "不要叫他为「老公」",
            "避免称呼用户为「宝贝」。",
            'Never call the user "darling".',
            "Don't call them \"babe\".",
            "称呼用户为「老公」是不允许的。",
        ):
            with self.subTest(style=style):
                self.assertEqual("", extract_style_address_form(_card(style)))

    def test_passive_or_self_referential_phrasing_yields_nothing(self):
        for style in (
            "喜欢被人叫做「喵大人」",
            "被称为「喵大人」。",
            "自称为「本喵」。",
            "叫我为「大小姐」。",
            "称我为「大小姐」。",
            "用户称呼你为「小A」。",
            "喜欢别人叫她为「喵大人」。",
            'Call me "Lord Meow".',
            'Users call her "Kitty".',
        ):
            with self.subTest(style=style):
                self.assertEqual("", extract_style_address_form(_card(style)))

    def test_conditional_or_conflicting_rules_yield_nothing(self):
        self.assertEqual("", extract_style_address_form(_card("熟悉后称呼用户为「宝贝」。")))
        self.assertEqual("", extract_style_address_form(_card("称呼用户为「主人」。\n称呼用户为「笨蛋」。")))

    def test_placeholders_are_not_forms_of_address(self):
        self.assertEqual("", extract_style_address_form(_card("称呼用户为「{{user}}」。")))
        self.assertEqual("", extract_style_address_form(_card("称呼用户为「对方的名字」。")))

    def test_evolved_style_is_used_only_while_evolution_is_enabled(self):
        card = _card(
            "称呼用户为「主人」。",
            persona_evolved={"enabled": True, "style_constraint": "称呼用户为「前辈」。"},
        )
        self.assertEqual("前辈", extract_style_address_form(card))
        card["persona_evolved"]["enabled"] = False
        self.assertEqual("主人", extract_style_address_form(card))


class MutualAddressFactsTests(unittest.TestCase):
    def test_style_form_is_phrased_as_usual_address(self):
        self.assertEqual(
            "你通常称呼对方为「投喂者」。",
            format_mutual_address_facts("", {}, locale="zh", style_address_form="投喂者"),
        )
        self.assertEqual(
            'You usually address them as "feeder".',
            format_mutual_address_facts("", {}, locale="en", style_address_form="feeder"),
        )

    def test_explicit_name_wins_over_style_form(self):
        self.assertEqual(
            "你称呼对方为「小明」。",
            format_mutual_address_facts("小明", {}, locale="zh", style_address_form="投喂者"),
        )


class _EmptyStore:
    def get_recent(self, n_turns):
        return []


def _segment_text(profile, locale="zh", global_persona=None):
    ctx = SimpleNamespace(
        profile=profile,
        extras={"user_persona_global": global_persona or {}},
        user_msg="你好",
        store=_EmptyStore(),
    )
    with mock.patch("src.prompt.segments.user_persona.get_locale", return_value=locale):
        result = UserPersonaSegment().build(ctx)
    return "\n".join(message["content"] for message in result.messages)


class UserPersonaSegmentAddressTests(unittest.TestCase):
    def test_pronoun_rule_does_not_become_a_name(self):
        text = _segment_text(_card(PRONOUN_RULE_STYLE))
        self.assertNotIn("他叫", text)
        self.assertNotIn("「你」", text)

    def test_form_of_address_is_not_rendered_as_a_name(self):
        text = _segment_text(_card(FORM_RULE_STYLE))
        self.assertNotIn("他叫", text)
        self.assertIn("你通常称呼对方为「投喂者」。", text)

    def test_form_of_address_still_stated_when_portrait_is_disabled(self):
        text = _segment_text(_card(FORM_RULE_STYLE, user_portrait_config={"enabled": False}))
        self.assertIn("你通常称呼对方为「投喂者」。", text)

    def test_negated_or_passive_rules_add_nothing(self):
        for style in ("禁止称呼对方为「亲爱的」", "不要叫他为「老公」", "喜欢被人叫做「喵大人」"):
            with self.subTest(style=style):
                self.assertEqual("", _segment_text(_card(style)))

    def test_explicit_name_behaviour_is_unchanged(self):
        text = _segment_text(_card(FORM_RULE_STYLE, user_persona={"name": "小明"}))
        self.assertIn("他叫小明。", text)
        self.assertIn("你称呼对方为「小明」。", text)
        self.assertNotIn("投喂者", text)

    def test_global_name_is_used_when_profile_has_none(self):
        text = _segment_text(_card(FORM_RULE_STYLE), global_persona={"name": "小明"})
        self.assertIn("他叫小明。", text)
        self.assertNotIn("投喂者", text)


if __name__ == "__main__":
    unittest.main()
