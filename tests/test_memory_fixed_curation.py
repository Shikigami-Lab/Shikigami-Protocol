from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from src.memory.curation import CurationConflict, MemoryCuration
from src.memory.curation_plan import validate_review
from src.memory.long_term_store import LongTermFact, LongTermStore, fact_action_revision


class FixedCurationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())
        self.store = LongTermStore(self.root)
        revision, cursors, _ = self.store.extraction_snapshot()
        rows = [
            LongTermFact(id="a", content="某次说了晚安，便去休息。", source="auto", is_manual=False, updated_at=1),
            LongTermFact(id="b", content="用户想用显卡运行本地模型。随后聊了几句闲话。", source="auto",
                         is_manual=False, updated_at=2),
            LongTermFact(id="c", content="用户有一个明确称呼。", kind="address", source="auto", is_manual=False),
        ]
        self.assertTrue(self.store.apply_extraction(revision, rows, cursors))
        self.curation = MemoryCuration("fixture", self.store)

    async def test_browse_is_free_and_focus_reuses_same_saved_judgment(self):
        decide = AsyncMock(return_value=[{
            "fact_id": "a", "action": "archive", "reason": "已经结束的当晚过场",
            "future_use": "", "evidence_quote": "某次说了晚安，便去休息。", "archive_reason": "ephemeral",
        }])
        page = await self.curation.preview_bulk_fixed(decide, model_key="test-model")
        decide.assert_not_awaited()
        self.assertEqual(page["items"][0]["action"], "pending")
        page = await self.curation.preview_bulk_fixed(decide, model_key="test-model", fact_id="a")
        self.assertEqual(page["items"][0]["action"], "archive")
        self.assertEqual([row.id for row in decide.await_args.args[0]], ["a"])
        reopened = MemoryCuration("fixture", LongTermStore(self.root))
        again = await reopened.preview_bulk_fixed(decide, model_key="test-model", fact_id="a")
        self.assertEqual(again["items"][0], page["items"][0])
        decide.assert_awaited_once()
        preview = reopened.promote_bulk(again["page_token"], "a")
        receipt = reopened.apply(preview["preview_id"], ["a"])
        self.assertEqual(receipt["archived"], 1)
        self.assertTrue(self.store.get_by_id("a").archived)

    async def test_update_preview_preserves_same_id_and_restorable_full_version(self):
        content = self.store.get_by_id("b").content
        decide = AsyncMock(return_value=[{
            "fact_id": "b", "action": "update", "reason": "保留硬件用途，移除闲聊",
            "future_use": "询问本地模型硬件", "evidence_quote": "用户想用显卡运行本地模型。",
            "keep_text": "用户想用显卡运行本地模型。",
        }])
        preview = await self.curation.preview_fixed(decide, model_key="test-model", fact_id="b")
        self.assertEqual(preview["items"][0]["action"], "update")
        self.assertEqual(self.store.get_by_id("b").content, content)
        change, = self.curation.pending_updates(preview["preview_id"], ["b"])
        self.assertEqual((change.id, change.content), ("b", "用户想用显卡运行本地模型。"))
        receipt = self.curation.apply(preview["preview_id"], ["b"])
        self.assertEqual(receipt["trimmed"], 1)
        fact = self.store.get_by_id("b")
        self.assertEqual(fact.content, change.content)
        self.assertEqual(fact.versions[-1]["content"], content)
        self.assertTrue(self.store.restore_version(fact.id, 0, fact_action_revision(fact)))
        self.assertEqual(self.store.get_by_id("b").content, content)

    def test_model_cannot_archive_a_privacy_or_speech_rule_in_an_untyped_note(self):
        texts = (
            "角色被禁攻击言语，其余只写了晚安。",
            "项目代码公开，但角色资料不可开源。",
        )
        for text in texts:
            with self.subTest(text=text):
                subject = LongTermFact(text, id="rule", source="auto", is_manual=False)
                result, = validate_review([subject], [{
                    "fact_id": subject.id, "action": "archive", "reason": "只有当场情节",
                    "future_use": "", "evidence_quote": text, "archive_reason": "ephemeral",
                }])
                self.assertEqual(result["action"], "keep")

    def test_model_cannot_archive_a_standing_preference_or_trim_a_scope_exception(self):
        keep = LongTermFact("用户偏好黑咖啡。", id="coffee", source="auto", is_manual=False)
        result, = validate_review([keep], [{
            "fact_id": keep.id, "action": "archive", "reason": "一次对话", "future_use": "",
            "evidence_quote": keep.content, "archive_reason": "ephemeral",
        }])
        self.assertEqual(result["action"], "keep")
        mixed = LongTermFact("用户平时偏好中文。面试时用英语。其他闲聊。", id="language",
                             source="auto", is_manual=False)
        result, = validate_review([mixed], [{
            "fact_id": mixed.id, "action": "update", "reason": "保留偏好",
            "future_use": "选择语言", "evidence_quote": "用户平时偏好中文。",
            "keep_text": "用户平时偏好中文。",
        }])
        self.assertEqual(result["action"], "keep")

    async def test_daily_preview_only_suggestion_does_not_clear_old_note_or_invalidate_cache(self):
        revision, cursors, facts, state = self.store.curation_snapshot()
        facts[1].review_note = "An older explicit review note"
        self.assertTrue(self.store.apply_curation(revision, facts[1:], cursors, state))
        decide = AsyncMock(return_value=[{
            "fact_id": "b", "action": "update", "reason": "保留硬件用途",
            "future_use": "询问硬件", "evidence_quote": "用户想用显卡运行本地模型。",
            "keep_text": "用户想用显卡运行本地模型。",
        }])
        await self.curation.run_daily("2026-11-07", decide, decide_dialogue=AsyncMock(), model_key="fixture")
        self.assertEqual(self.store.get_by_id("b").review_note, "An older explicit review note")
        page = await self.curation.preview_bulk_fixed(decide, model_key="fixture")
        self.assertEqual(page["items"][0]["action"], "update")
        decide.assert_awaited_once()

    async def test_fixed_preview_refuses_stale_model_result(self):
        async def changed(rows):
            self.assertTrue(self.store.update(rows[0].id, content="用户明确保留这条。"))
            return [{"fact_id": rows[0].id, "action": "keep", "reason": "测试",
                     "future_use": "测试用途", "evidence_quote": rows[0].content}]
        with self.assertRaises(CurationConflict):
            await self.curation.preview_fixed(changed, fact_id="b")
