"""会话内滚动摘要：存储、压缩门控、强制压缩（重置前）、段注入的离线验收。

摘要与日期无关：只记录生成时间，不跨天重置、不交接日摘要。
"""
import json
from datetime import datetime
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch

from src.llm.openai_compat import OpenAICompatProvider
from src.memory.conversation_store import ConversationStore
from src.memory.memory_manager import MemoryManager
from src.memory.rolling_store import RollingSummaryStore

PROFILE = "fixture"
DAY = "2026-11-07"


class RollingSummaryStoreTests(TestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())

    def test_update_get_roundtrip_and_persistence(self):
        store = RollingSummaryStore(self.root)
        self.assertIsNone(store.get())
        store.update("聊了项目进度", 42)
        data = store.get()
        self.assertEqual(data["summary"], "聊了项目进度")
        self.assertEqual(data["covered_up_to_id"], 42)
        self.assertIn("updated_at", data)
        self.assertNotIn("date", data)
        reloaded = RollingSummaryStore(self.root)
        self.assertEqual(reloaded.get()["summary"], "聊了项目进度")

    def test_legacy_date_field_is_dropped_on_load(self):
        with open(self.root + "/rolling_summary.json", "w", encoding="utf-8") as f:
            json.dump({"date": "2026-11-07", "summary": "旧格式摘要",
                       "covered_up_to_id": 7, "updated_at": 1.0}, f)
        data = RollingSummaryStore(self.root).get()
        self.assertEqual(data["summary"], "旧格式摘要")
        self.assertNotIn("date", data)

    def test_clear(self):
        store = RollingSummaryStore(self.root)
        self.assertFalse(store.clear())
        store.update("摘要", 1)
        self.assertTrue(store.clear())
        self.assertIsNone(store.get())
        self.assertIsNone(RollingSummaryStore(self.root).get())


class RollingSummaryManagerTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())
        self.enterContext(patch("src.memory.memory_manager.log_memory_startup"))
        self.manager = MemoryManager(PROFILE, self.root, profile_memory_cfg={
            "enabled": True,
            "rolling_summary_enabled": True,
            "rolling_summary_interval_turns": 5,
            "rolling_summary_keep_turns": 10,
            "rolling_summary_max_chars": 800,
            "extraction_llm_preset": "memory",
        })
        self.store = ConversationStore(self.root + "/chat", max_turns=0)
        self.addCleanup(self.store.close)
        self.preset = {"model": "fixture-memory"}
        self.app = SimpleNamespace(state=SimpleNamespace(
            config=SimpleNamespace(
                user_name="用户",
                get_llm_preset=lambda name: self.preset,
                get_active_llm_preset=lambda: self.preset,
            ),
            session_manager=SimpleNamespace(list_sessions=lambda: []),
            memory_managers={PROFILE: self.manager},
        ))
        self.provider = object.__new__(OpenAICompatProvider)
        self.get_provider = self.enterContext(patch("src.llm.registry.get_provider", return_value=self.provider))
        self.calls = []
        self.reply("这是一段滚动摘要。")

    def reply(self, response):
        async def stream(messages):
            self.calls.append(messages)
            yield response
        self.provider.stream_chat = stream

    def seed_turns(self, n, text="聊了会儿天"):
        for i in range(n):
            self.store.append("user", f"{text} {i}")
            self.store.append("assistant", f"好的 {i}")

    async def compact(self, **kw):
        return await self.manager.maybe_update_rolling_summary(self.store, {}, self.app, **kw)

    async def test_disabled_gate(self):
        self.manager._cfg["rolling_summary_enabled"] = False
        self.seed_turns(20)
        self.assertFalse(await self.compact())
        self.get_provider.assert_not_called()

    async def test_none_preset_gate(self):
        self.manager._cfg["extraction_llm_preset"] = "__none__"
        self.seed_turns(20)
        self.assertFalse(await self.compact())
        self.get_provider.assert_not_called()

    async def test_below_interval_does_not_compact(self):
        # 10 轮：keep_turns=10 的窗口正好覆盖全部，窗口外无积压
        self.seed_turns(10)
        self.assertFalse(await self.compact())
        self.get_provider.assert_not_called()

    async def test_compacts_backlog_outside_window(self):
        self.seed_turns(20)
        self.assertTrue(await self.compact())
        self.get_provider.assert_called_once_with(self.preset)
        data = self.manager.rolling.get()
        self.assertEqual(data["summary"], "这是一段滚动摘要。")
        # 窗口内（最近 10 轮）原文不进摘要输入
        conv = self.calls[0][-1]["content"]
        self.assertIn("聊了会儿天 0", conv)       # 最早的在窗口外
        self.assertNotIn("聊了会儿天 19", conv)   # 最新的在窗口内
        covered = data["covered_up_to_id"]
        window = self.store.get_recent_with_ts(10)
        self.assertLess(covered, window[0]["id"])
        # 覆盖位置之后立即再调用：无新积压，不重复压缩
        self.assertFalse(await self.compact())
        self.assertEqual(len(self.calls), 1)

    async def test_merges_previous_summary_into_input(self):
        self.seed_turns(20)
        self.manager.rolling.update("上一版摘要内容", 0)
        self.assertTrue(await self.compact())
        self.assertIn("上一版摘要内容", self.calls[0][-1]["content"])

    async def test_llm_error_keeps_state_unchanged(self):
        self.seed_turns(20)
        self.reply("[LLM Error: boom]")
        self.assertFalse(await self.compact())
        self.assertIsNone(self.manager.rolling.get())

    async def test_output_hard_capped(self):
        self.seed_turns(20)
        self.reply("长" * 2000)
        self.assertTrue(await self.compact())
        data = self.manager.rolling.get()
        self.assertLessEqual(len(data["summary"]), 800 + 1)

    async def test_force_all_compacts_everything_regardless_of_window(self):
        # 重置短期上下文前：全部对话都进压缩，不受窗口/间隔限制
        self.seed_turns(3)  # 远低于 interval=5，且全部在窗口内
        self.assertTrue(await self.compact(force_all=True))
        conv = self.calls[0][-1]["content"]
        self.assertIn("聊了会儿天 2", conv)  # 最新一轮也被压缩
        tail = self.store.memory_tail_id()
        self.assertEqual(self.manager.rolling.get()["covered_up_to_id"], tail)

    async def test_force_all_with_no_new_turns_is_noop(self):
        self.assertFalse(await self.compact(force_all=True))
        self.get_provider.assert_not_called()

    async def test_old_summary_survives_day_boundary(self):
        # 摘要与日期无关：旧摘要不会因为跨天被重置或拦截
        self.manager._cfg["day_summary_enabled"] = True
        self.manager.rolling.update("很久以前的摘要", 3)
        self.seed_turns(20)
        self.assertTrue(await self.compact())
        self.assertIn("很久以前的摘要", self.calls[0][-1]["content"])

    async def test_day_summary_does_not_touch_rolling(self):
        self.manager._cfg["day_summary_enabled"] = True
        self.manager.rolling.update("压缩内容", 999)
        ts = datetime(2026, 11, 7, 10).timestamp()
        msgs = [
            {"role": "user", "content": "晚上聊了什么", "sender": "", "timestamp": ts},
            {"role": "assistant", "content": "聊了很多", "sender": "fixture", "timestamp": ts + 1},
        ]
        result = await self.manager.generate_day_summary(DAY, self.store, {}, self.app,
                                                         day_msgs_override=msgs)
        self.assertIsNotNone(result)
        conv = self.calls[0][-1]["content"]
        self.assertNotIn("【当天滚动摘要】", conv)  # 不再并入日摘要输入
        self.assertIsNotNone(self.manager.rolling.get())  # 日摘要生成后仍保留


class RollingSummarySegmentTests(TestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())
        self.manager = MemoryManager(PROFILE, self.root, profile_memory_cfg={
            "enabled": True, "rolling_summary_enabled": True,
        })
        self.app = SimpleNamespace(state=SimpleNamespace(memory_managers={PROFILE: self.manager}))
        from src.prompt.segments.rolling_summary import RollingSummarySegment
        self.segment = RollingSummarySegment()
        self.ctx = SimpleNamespace(extras={"app": self.app},
                                   session=SimpleNamespace(profile_id=PROFILE))

    def test_fires_with_summary_regardless_of_age(self):
        self.manager.rolling.update("早些时候的摘要", 10)
        result = self.segment.build(self.ctx)
        self.assertTrue(result.fired)
        self.assertIn("【早些时候的对话摘要】", result.messages[0]["content"])
        self.assertIn("早些时候的摘要", result.messages[0]["content"])

    def test_silent_when_disabled_or_empty(self):
        self.assertFalse(self.segment.build(self.ctx).fired)
        self.manager._cfg["rolling_summary_enabled"] = False
        self.manager.rolling.update("摘要", 10)
        self.assertFalse(self.segment.build(self.ctx).fired)

    def test_segment_metadata(self):
        self.assertEqual(self.segment.priority, 87)
        self.assertEqual(self.segment.requires, ("feature:memory",))
        self.assertTrue(self.segment.is_readonly)
        self.assertEqual(self.segment.inject_into, "chat")


class RollingSummaryConfigTests(TestCase):
    def test_defaults_and_bounds(self):
        from src.api.settings_ext import MemoryConfigBody, ProfileMemoryConfigBody
        body = MemoryConfigBody()
        self.assertFalse(body.rolling_summary_enabled)
        self.assertEqual(body.rolling_summary_interval_turns, 60)
        self.assertEqual(body.rolling_summary_keep_turns, 60)
        self.assertEqual(body.rolling_summary_max_chars, 800)
        profile_body = ProfileMemoryConfigBody()
        self.assertIsNone(profile_body.rolling_summary_enabled)
        from src.config.app_config import AppConfig  # noqa: F401  # 默认键存在性由 MemoryManager 默认值覆盖

    def test_manager_defaults(self):
        with TemporaryDirectory() as root:
            with patch("src.memory.memory_manager.log_memory_startup"):
                mgr = MemoryManager(PROFILE, root)
            self.assertFalse(mgr._cfg["rolling_summary_enabled"])
            self.assertEqual(mgr._cfg["rolling_summary_interval_turns"], 60)
            self.assertEqual(mgr._cfg["rolling_summary_keep_turns"], 60)
            self.assertEqual(mgr._cfg["rolling_summary_max_chars"], 800)
