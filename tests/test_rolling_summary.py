"""会话内滚动摘要：存储、压缩门控、跨天交接、段注入的离线验收。"""
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
YESTERDAY = "2020-01-01"  # 必定不是今天


class RollingSummaryStoreTests(TestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())

    def test_update_get_roundtrip_and_persistence(self):
        store = RollingSummaryStore(self.root)
        self.assertIsNone(store.get())
        store.update("聊了项目进度", 42, DAY)
        data = store.get()
        self.assertEqual(data["summary"], "聊了项目进度")
        self.assertEqual(data["covered_up_to_id"], 42)
        self.assertEqual(data["date"], DAY)
        reloaded = RollingSummaryStore(self.root)
        self.assertEqual(reloaded.get()["summary"], "聊了项目进度")

    def test_reset_and_clear_if_date(self):
        store = RollingSummaryStore(self.root)
        store.update("摘要", 1, DAY)
        self.assertFalse(store.clear_if_date("2026-01-01"))
        self.assertIsNotNone(store.get())
        self.assertTrue(store.clear_if_date(DAY))
        self.assertIsNone(store.get())
        store.update("摘要", 1, DAY)
        store.reset("2026-11-08")
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

    async def compact(self):
        return await self.manager.maybe_update_rolling_summary(self.store, {}, self.app)

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
        self.manager.rolling.update("上一版摘要内容", 0, self._today())
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

    async def test_cross_day_waits_for_day_summary_handoff(self):
        self.manager._cfg["day_summary_enabled"] = True
        self.manager.rolling.update("旧摘要", 3, YESTERDAY)
        self.seed_turns(20)
        self.assertFalse(await self.compact())
        self.assertIsNotNone(self.manager.rolling.get())  # 未重置
        self.get_provider.assert_not_called()

    async def test_cross_day_resets_when_day_summary_off(self):
        self.manager.rolling.update("旧摘要", 3, YESTERDAY)
        self.assertFalse(await self.compact())  # 无积压，但跨天重置应已发生
        self.assertIsNone(self.manager.rolling.get())

    async def test_day_summary_handoff_merges_and_clears(self):
        self.manager._cfg["day_summary_enabled"] = True
        self.manager.rolling.update("压缩内容", 999, DAY)
        ts = datetime(2026, 11, 7, 10).timestamp()
        msgs = [
            {"role": "user", "content": "晚上聊了什么", "sender": "", "timestamp": ts},
            {"role": "assistant", "content": "聊了很多", "sender": "fixture", "timestamp": ts + 1},
        ]
        result = await self.manager.generate_day_summary(DAY, self.store, {}, self.app,
                                                         day_msgs_override=msgs)
        self.assertIsNotNone(result)
        conv = self.calls[0][-1]["content"]
        self.assertIn("【当天滚动摘要】", conv)
        self.assertIn("压缩内容", conv)
        self.assertIsNone(self.manager.rolling.get())  # 交接后清除

    def _today(self):
        from datetime import date
        return date.today().strftime("%Y-%m-%d")


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

    def test_fires_with_today_summary(self):
        from datetime import date
        self.manager.rolling.update("早些时候的摘要", 10, date.today().strftime("%Y-%m-%d"))
        result = self.segment.build(self.ctx)
        self.assertTrue(result.fired)
        self.assertIn("【今天早些时候】", result.messages[0]["content"])
        self.assertIn("早些时候的摘要", result.messages[0]["content"])

    def test_silent_when_disabled_or_empty(self):
        self.assertFalse(self.segment.build(self.ctx).fired)
        self.manager.rolling.update("摘要", 10, YESTERDAY)  # 昨天的不注入
        self.assertFalse(self.segment.build(self.ctx).fired)
        self.manager._cfg["rolling_summary_enabled"] = False
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
        self.assertEqual(body.rolling_summary_interval_turns, 20)
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
            self.assertEqual(mgr._cfg["rolling_summary_interval_turns"], 20)
            self.assertEqual(mgr._cfg["rolling_summary_keep_turns"], 60)
            self.assertEqual(mgr._cfg["rolling_summary_max_chars"], 800)
