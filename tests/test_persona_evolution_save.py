import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from src.core import persona_evolution as pe


def _reply(**fields):
    return json.dumps(fields, ensure_ascii=False)


class ParseJsonObjectTests(unittest.TestCase):
    def test_tolerates_prose_fences_and_raw_newlines(self):
        self.assertEqual({"a": 1}, pe._parse_json_object('Here:\n```json\n{"a": 1}\n```\nDone.'))
        self.assertEqual("x\ny", pe._parse_json_object('{"base_prompt": "x\ny"}')["base_prompt"])

    def test_raises_without_an_object(self):
        with self.assertRaises(ValueError):
            pe._parse_json_object("no json here")


class SectionHeadingTests(unittest.TestCase):
    def test_short_labels_are_headings(self):
        text = "【身份】\nA\n[Core Identity]\nB\n## Behavior\nC"
        self.assertEqual(["【身份】", "[Core Identity]", "## Behavior"], pe._extract_section_headings(text))

    def test_bracketed_content_lines_are_not_headings(self):
        for line in (
            "[Mochi's persona: proud, lazy; likes fish]",
            "【身份：猫又，性格：傲娇】",
            "[{{char}}'s Traits]",
            '[character("Mochi") { Personality("proud" + "lazy") }]',
        ):
            with self.subTest(line=line):
                self.assertEqual([], pe._extract_section_headings(line))

    def test_editing_a_plist_line_passes_validation(self):
        ok, _, reason = pe._validate_evolution_output(
            original_base="[Mochi's persona: proud, lazy]",
            new_base="[Mochi's persona: proud, lazy, curious]",
            current_style="short",
            new_style="short",
        )
        self.assertTrue(ok, reason)


class TriggerEvolutionSaveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.profiles = tmp.name
        self.storage_root = os.path.join(tmp.name, "p1")
        os.makedirs(self.storage_root)
        patcher = mock.patch.object(pe, "_PROFILES_DIR", self.profiles)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_card(self, card):
        with open(os.path.join(self.profiles, "p1.json"), "w", encoding="utf-8") as f:
            json.dump(card, f, ensure_ascii=False)

    def _read_card(self):
        with open(os.path.join(self.profiles, "p1.json"), encoding="utf-8") as f:
            return json.load(f)

    async def _run(self, replies, during_call=None):
        roles = []

        async def fake_llm(messages, app, role, profile_id):
            roles.append(role)
            if during_call and len(roles) == 1:
                during_call()
            return replies[len(roles) - 1]

        with mock.patch.object(pe, "_llm_call", side_effect=fake_llm), \
                mock.patch.object(pe, "_get_recent_facts_text", return_value="- [preference] likes tea"), \
                mock.patch.object(pe, "_get_recent_conversation_text", return_value=""), \
                mock.patch.object(pe, "_get_affinity_status", return_value="friend"), \
                mock.patch("src.core.user_portrait._resolve_user_intro", return_value=("", "")):
            entry = await pe.trigger_evolution("p1", self.storage_root, SimpleNamespace())
        return entry, roles

    def _base_card(self):
        return {
            "base_prompt": "【身份】\nA cat.",
            "style_constraint": "short",
            "persona_evolved": {"core_anchor": "proud"},
        }

    async def test_edits_saved_during_the_model_call_are_kept(self):
        self._write_card(self._base_card())

        def user_edits():
            card = self._read_card()
            card["style_constraint"] = "edited while evolving"
            card["memory_config"] = {"extraction_frequency": 3}
            self._write_card(card)

        reply = _reply(base_prompt="【身份】\nA proud cat.", style_constraint="short",
                       change_summary="- prouder", reason="r", anchor_preserved=True)
        entry, _ = await self._run([reply], during_call=user_edits)

        self.assertIsNotNone(entry)
        card = self._read_card()
        self.assertEqual("edited while evolving", card["style_constraint"])
        self.assertEqual({"extraction_frequency": 3}, card["memory_config"])
        self.assertEqual("【身份】\nA proud cat.", card["persona_evolved"]["base_prompt"])
        self.assertEqual(1, card["persona_evolved"]["evolution_count"])

    async def test_over_cap_persona_is_compressed_when_the_model_skips(self):
        card = self._base_card()
        card["base_prompt"] = "【身份】\n猫。"
        card["persona_evolved"]["base_prompt"] = "【身份】\n" + "猫。" * 1000  # over the 1500-char soft cap
        self._write_card(card)

        skip = _reply(anchor_preserved=False, reason="no durable evidence")
        compressed = _reply(base_prompt="【身份】\n猫。", style_constraint="short",
                            change_summary="- 精简重复描述", reason="over cap", anchor_preserved=True)
        entry, roles = await self._run([skip, compressed])

        self.assertEqual(["persona_evolve", "persona_evolve_compress"], roles)
        self.assertIsNotNone(entry)
        self.assertEqual("【身份】\n猫。", self._read_card()["persona_evolved"]["base_prompt"])

    async def test_skip_under_the_cap_writes_nothing(self):
        card = self._base_card()
        self._write_card(card)

        entry, roles = await self._run([_reply(anchor_preserved=False, reason="thin evidence")])

        self.assertIsNone(entry)
        self.assertEqual(["persona_evolve"], roles)
        self.assertEqual(card, self._read_card())

    async def test_unchanged_result_is_not_saved(self):
        card = self._base_card()
        self._write_card(card)

        same = _reply(base_prompt="【身份】\nA cat.", style_constraint="short",
                      change_summary="-", reason="-", anchor_preserved=True)
        entry, _ = await self._run([same])

        self.assertIsNone(entry)
        self.assertEqual(card, self._read_card())

    async def test_evolution_disabled_during_the_call_is_not_saved(self):
        self._write_card(self._base_card())

        def disable():
            card = self._read_card()
            card["persona_evolved"]["enabled"] = False
            self._write_card(card)

        reply = _reply(base_prompt="【身份】\nA proud cat.", style_constraint="short",
                       change_summary="- prouder", reason="r", anchor_preserved=True)
        entry, _ = await self._run([reply], during_call=disable)

        self.assertIsNone(entry)
        self.assertNotIn("base_prompt", self._read_card()["persona_evolved"])


if __name__ == "__main__":
    unittest.main()
