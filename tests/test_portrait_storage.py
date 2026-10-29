"""Portrait storage: layer edits, lost-update safety, address-form watermark and labels.

Everything runs against a temporary profiles directory with synthetic data.
"""
import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import src.core.persona_evolution as persona_evolution
import src.core.user_portrait as user_portrait

PROFILE_ID = "portrait_test"
FORM_RULE_STYLE = "【风格约束】\n称呼用户为「投喂者」或直呼其名；极高信任后才自然改口。"


def _app(user_name="小明"):
    config = SimpleNamespace(user_name=user_name, user_persona={})
    return SimpleNamespace(state=SimpleNamespace(config=config))


def _turn(role, content, ts, sender=""):
    return {"role": role, "content": content, "sender": sender, "timestamp": ts}


class _TempProfileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.profiles_dir = tmp.name
        self.storage_root = os.path.join(self.profiles_dir, PROFILE_ID)
        os.makedirs(self.storage_root)
        for target, name, value in (
            (user_portrait, "_PROFILES_DIR", self.profiles_dir),
            (user_portrait, "get_locale", lambda: "zh"),
            (persona_evolution, "_PROFILES_DIR", self.profiles_dir),
            (persona_evolution, "get_locale", lambda: "zh"),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_card(self, card):
        with open(os.path.join(self.profiles_dir, f"{PROFILE_ID}.json"), "w", encoding="utf-8") as f:
            json.dump(card, f, ensure_ascii=False)

    def read_card(self):
        with open(os.path.join(self.profiles_dir, f"{PROFILE_ID}.json"), "r", encoding="utf-8") as f:
            return json.load(f)


class RecentContextStatusTests(unittest.TestCase):
    def test_expiry_matches_ttl(self):
        now = 1_000_000.0
        block = {"recent_context": {"content": "准备考试", "updated_at": now - 3600}}
        status = user_portrait.recent_context_status(block, 72, now)
        self.assertEqual(int(now - 3600 + 72 * 3600), status["expires_at"])
        self.assertFalse(status["expired"])
        self.assertTrue(user_portrait.recent_context_status(block, 0.5, now)["expired"])

    def test_no_ttl_and_legacy_string_never_expire(self):
        now = 1_000_000.0
        block = {"recent_context": {"content": "准备考试", "updated_at": 1}}
        self.assertEqual(0, user_portrait.recent_context_status(block, 0, now)["expires_at"])
        legacy = user_portrait.recent_context_status({"recent_context": "旧格式"}, 72, now)
        self.assertEqual(("旧格式", False), (legacy["content"], legacy["expired"]))

    def test_ttl_default_and_bad_values(self):
        self.assertEqual(72.0, user_portrait.recent_context_ttl_hours({}))
        self.assertEqual(72.0, user_portrait.recent_context_ttl_hours({"recent_context_ttl_hours": "x"}))
        self.assertEqual(12.0, user_portrait.recent_context_ttl_hours({"recent_context_ttl_hours": 12}))


class PortraitLayerEditTests(_TempProfileTest):
    def test_clear_recent_context(self):
        self.write_card({"user_portrait": {
            "content": "稳定画像",
            "recent_context": {"content": "准备考试", "updated_at": 100},
            "last_input_signature": "abc",
        }})
        self.assertTrue(user_portrait.clear_recent_context(PROFILE_ID))
        portrait = self.read_card()["user_portrait"]
        self.assertEqual({}, portrait["recent_context"])
        self.assertEqual("", portrait["last_input_signature"])
        self.assertEqual("稳定画像", portrait["content"])

    def test_delete_contextual_detail(self):
        keep = {"summary": "Uses dark mode for coding", "keywords": ["editor", "theme"]}
        self.write_card({"user_portrait": {"contextual_details": [
            {"summary": "Prefers window seats", "keywords": ["flight", "seat"]},
            keep,
        ]}})
        self.assertTrue(user_portrait.delete_contextual_detail(PROFILE_ID, "prefers WINDOW seats"))
        self.assertEqual([keep], self.read_card()["user_portrait"]["contextual_details"])
        self.assertFalse(user_portrait.delete_contextual_detail(PROFILE_ID, "missing"))
        self.assertFalse(user_portrait.delete_contextual_detail("no_such_profile", "Uses dark mode for coding"))


class PortraitEndpointTests(_TempProfileTest):
    def setUp(self):
        super().setUp()
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            import src.api.settings_ext as settings_ext
        except Exception as exc:  # pragma: no cover - depends on optional test deps
            self.skipTest(f"API test dependencies unavailable: {exc}")
        patcher = mock.patch.object(settings_ext, "_PROFILES_DIR", self.profiles_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(settings_ext.router)
        self.client = TestClient(app)

    def test_layers_are_exposed_and_editable(self):
        import time as _time
        now = int(_time.time())
        self.write_card({"user_portrait": {
            "content": "稳定画像",
            "recent_context": {"content": "准备考试", "updated_at": now},
            "contextual_details": [{"summary": "搬家定在月底", "keywords": ["搬家", "月底"]}],
        }})
        data = self.client.get(f"/profiles/{PROFILE_ID}/user_portrait").json()
        self.assertEqual("准备考试", data["recent_context"]["content"])
        self.assertEqual(now + 72 * 3600, data["recent_context"]["expires_at"])
        self.assertFalse(data["recent_context"]["expired"])
        self.assertEqual([{"summary": "搬家定在月底", "keywords": ["搬家", "月底"]}], data["contextual_details"])
        self.assertEqual(72, data["config"]["recent_context_ttl_hours"])

        res = self.client.post(f"/profiles/{PROFILE_ID}/user_portrait/contextual_details/delete",
                               json={"summary": "搬家定在月底"})
        self.assertEqual({"ok": True}, res.json())
        res = self.client.post(f"/profiles/{PROFILE_ID}/user_portrait/recent_context/clear")
        self.assertEqual({"ok": True}, res.json())

        data = self.client.get(f"/profiles/{PROFILE_ID}/user_portrait").json()
        self.assertEqual("", data["recent_context"]["content"])
        self.assertEqual([], data["contextual_details"])
        self.assertEqual(404, self.client.post("/profiles/missing/user_portrait/recent_context/clear").status_code)

    def test_baseline_review_interval_is_saved(self):
        self.write_card({"base_prompt": "x", "persona_evolved": {}})
        res = self.client.put(f"/profiles/{PROFILE_ID}/persona_evolution", json={"baseline_review_interval": 5})
        self.assertEqual(200, res.status_code)
        self.assertEqual(5, self.read_card()["persona_evolved"]["baseline_review_interval"])


class ConversationLabelTests(unittest.TestCase):
    def test_every_user_line_uses_the_current_label(self):
        turns = [
            _turn("user", "旧消息", 1),
            _turn("user", "改名前", 2, sender="旧名字"),
            _turn("assistant", "回复", 3, sender="Mochi"),
            _turn("assistant", "无发送者", 4),
            _turn("user", "现在", 5, sender="小明"),
        ]
        self.assertEqual(
            "小明: 旧消息\n小明: 改名前\nMochi: 回复\nAI: 无发送者\n小明: 现在",
            user_portrait._format_conversation(turns, "小明"),
        )


class AddressFormRefreshTests(_TempProfileTest):
    def run_refresh(self, turns, replies, app=None):
        calls = []

        async def fake_llm(messages, _app, role, profile_id):
            calls.append(messages[0]["content"])
            reply = replies.pop(0)
            return reply(self) if callable(reply) else reply

        with mock.patch.object(user_portrait, "_get_recent_turns", return_value=turns), \
                mock.patch.object(user_portrait, "_llm_call", side_effect=fake_llm):
            result = asyncio.run(user_portrait.refresh_address_forms(PROFILE_ID, self.storage_root, app or _app()))
        return result, calls

    def forms(self):
        block = self.read_card()["user_portrait"]["address_forms"]
        return {f["form"]: f["count"] for f in block["forms"]}

    def test_edits_saved_during_the_llm_call_are_kept(self):
        self.write_card({
            "display_name": "Mochi",
            "base_prompt": "original",
            "user_portrait": {"address_forms": {
                "forms": [{"form": "小A", "count": 2}, {"form": "误识别", "count": 1}],
                "updated_at": 50,
            }},
        })

        def edit_during_call(test):
            card = test.read_card()
            card["base_prompt"] = "edited during call"
            test.write_card(card)
            user_portrait.delete_address_form(PROFILE_ID, "误识别")
            return '[{"form": "小A", "count": 1, "evidence": "小A，在吗"}]\n以上'

        turns = [_turn("user", "小A，在吗", 100.0, "小明"), _turn("assistant", "在", 101.0, "Mochi")]
        result, calls = self.run_refresh(turns, [edit_during_call])

        card = self.read_card()
        self.assertEqual("edited during call", card["base_prompt"])
        self.assertEqual({"小A": 3}, self.forms())
        self.assertEqual(101.0, card["user_portrait"]["address_forms"]["scanned_until"])
        self.assertEqual(["小A"], [f["form"] for f in result])
        self.assertIn("以「小明」开头的行", calls[0])
        self.assertIn("小明: 小A，在吗", calls[0])

    def test_messages_are_counted_only_once(self):
        self.write_card({"display_name": "Mochi", "user_portrait": {}})
        first = [_turn("user", "小A，晚安", 100.0), _turn("assistant", "晚安", 101.0)]
        self.run_refresh(first, ['[{"form": "小A", "count": 1}]'])
        self.assertEqual({"小A": 1}, self.forms())

        result, calls = self.run_refresh(first, [])
        self.assertIsNone(result)
        self.assertEqual([], calls)
        self.assertEqual({"小A": 1}, self.forms())

        second = first + [_turn("user", "小A，早", 200.0)]
        _, calls = self.run_refresh(second, ['[{"form": "小A", "count": 1}]'])
        self.assertIn("小明: 小A，早", calls[0])
        self.assertNotIn("小明: 小A，晚安", calls[0])
        self.assertNotIn(": 晚安", calls[0])
        self.assertEqual({"小A": 2}, self.forms())

    def test_table_without_watermark_resumes_after_its_last_update(self):
        self.write_card({"user_portrait": {"address_forms": {
            "forms": [{"form": "小A", "count": 5}],
            "updated_at": 150,
        }}})
        turns = [_turn("user", "小A，旧消息", 100.0), _turn("user", "小A，新消息", 200.0)]
        _, calls = self.run_refresh(turns, ['[{"form": "小A", "count": 1}]'])
        self.assertNotIn("旧消息", calls[0])
        self.assertIn("新消息", calls[0])
        self.assertEqual({"小A": 6}, self.forms())

    def test_empty_result_advances_watermark_but_unreadable_reply_does_not(self):
        self.write_card({"user_portrait": {}})
        turns = [_turn("user", "在吗", 100.0)]

        result, _ = self.run_refresh(turns, ["我没找到称呼"])
        self.assertIsNone(result)
        self.assertNotIn("address_forms", self.read_card()["user_portrait"])

        result, _ = self.run_refresh(turns, ["[]"])
        self.assertIsNone(result)
        self.assertEqual(100.0, self.read_card()["user_portrait"]["address_forms"]["scanned_until"])

        _, calls = self.run_refresh(turns, [])
        self.assertEqual([], calls)

    def test_concurrent_refresh_does_not_double_count(self):
        self.write_card({"user_portrait": {}})

        def other_refresh_finishes_first(test):
            card = test.read_card()
            card["user_portrait"]["address_forms"] = {
                "forms": [{"form": "小A", "count": 1}], "updated_at": 300, "scanned_until": 100.0,
            }
            test.write_card(card)
            return '[{"form": "小A", "count": 1}]'

        result, _ = self.run_refresh([_turn("user", "小A", 100.0)], [other_refresh_finishes_first])
        self.assertIsNone(result)
        self.assertEqual({"小A": 1}, self.forms())


class PortraitRefreshTests(_TempProfileTest):
    def test_refresh_keeps_concurrent_edits_and_separates_name_from_address(self):
        self.write_card({
            "display_name": "Mochi",
            "base_prompt": "【身份】\n一只猫",
            "style_constraint": FORM_RULE_STYLE,
            "user_portrait": {
                "content": "旧画像",
                "refresh_count": 2,
                "address_forms": {"forms": [{"form": "小A", "count": 3}], "updated_at": 10, "scanned_until": 10.0},
            },
        })
        captured = {}
        reply = "好的：\n```json\n" + json.dumps({
            "content": "新画像第一段\n\n新画像第二段",
            "recent_context": "最近在搬家",
            "contextual_details": [],
        }, ensure_ascii=False).replace("\\n", "\n") + "\n```"

        async def fake_llm(messages, _app, role, profile_id):
            captured["system"] = messages[0]["content"]
            card = self.read_card()
            card["display_name"] = "Mochi (renamed)"
            card["user_portrait"]["address_forms"]["forms"].append({"form": "猫猫", "count": 1})
            self.write_card(card)
            return reply

        with mock.patch.object(user_portrait, "_get_recent_conversation_text", return_value="小明: 你好"), \
                mock.patch.object(user_portrait, "_get_recent_day_summaries_text", return_value=""), \
                mock.patch.object(user_portrait, "_get_facts_text", return_value=""), \
                mock.patch.object(user_portrait, "refresh_address_forms", new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(user_portrait, "_llm_call", side_effect=fake_llm):
            entry = asyncio.run(user_portrait.trigger_portrait_refresh(
                PROFILE_ID, self.storage_root, _app(), force=True, triggered_by="manual",
            ))

        self.assertIsNotNone(entry)
        card = self.read_card()
        portrait = card["user_portrait"]
        self.assertEqual("Mochi (renamed)", card["display_name"])
        self.assertEqual(["小A", "猫猫"], [f["form"] for f in portrait["address_forms"]["forms"]])
        self.assertEqual("新画像第一段\n\n新画像第二段", portrait["content"])
        self.assertEqual("最近在搬家", portrait["recent_context"]["content"])
        self.assertEqual(3, portrait["refresh_count"])
        self.assertEqual("旧画像", entry["content_before"])

        self.assertIn("对方希望被称呼为：（用户尚未设置称呼）", captured["system"])
        self.assertIn("你通常称呼对方为「投喂者」。", captured["system"])
        self.assertNotIn("「投喂者」」", captured["system"])


class PersonaEvolutionPromptTests(_TempProfileTest):
    def test_evolution_prompt_uses_style_form_and_default_baseline_interval(self):
        self.write_card({
            "display_name": "Mochi",
            "base_prompt": "【身份】\n一只猫",
            "style_constraint": FORM_RULE_STYLE,
            "persona_evolved": {"core_anchor": "嘴硬心软", "evolution_count": 2},
        })
        captured = {}

        async def fake_llm(messages, _app, role, profile_id):
            captured["system"] = messages[0]["content"]
            return '{"anchor_preserved": false, "reason": "test"}'

        with mock.patch.object(persona_evolution, "_get_recent_facts_text", return_value="- [preference] 喜欢鱼"), \
                mock.patch.object(persona_evolution, "_get_recent_conversation_text", return_value=""), \
                mock.patch.object(persona_evolution, "_get_affinity_status", return_value="熟悉"), \
                mock.patch.object(persona_evolution, "_llm_call", side_effect=fake_llm):
            result = asyncio.run(persona_evolution.trigger_evolution(PROFILE_ID, self.storage_root, _app()))

        self.assertIsNone(result)
        self.assertEqual(3, persona_evolution.DEFAULT_BASELINE_REVIEW_INTERVAL)
        self.assertIn("你通常称呼对方为「投喂者」。", captured["system"])
        self.assertNotIn("你称呼对方为「投喂者」", captured["system"])
        self.assertIn("基线复核", captured["system"])


if __name__ == "__main__":
    unittest.main()
