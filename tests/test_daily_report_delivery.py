"""Offline coverage for the recurring report delivery boundary."""
import asyncio
from datetime import datetime
from functools import partial
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.session import Session
from src.tools.todo.store import TodoStore


class DailyReportDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.session = Session("reporter", "reporter", "Reporter", workspace.name)
        self.addCleanup(self.session.shutdown)
        self.manager = SimpleNamespace(list_sessions=lambda: [self.session],
                                       get_current=lambda: SimpleNamespace(id="other"))
        self.runtime = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(
                config=SimpleNamespace(get_active_llm_preset=lambda: {"model": "fixture", "base_url": "http://fixture"}),
                session_manager=self.manager,
            )),
            profile_loader=lambda _: {"base_prompt": "Synthetic persona"},
            store=SimpleNamespace(revision=lambda _: 3, task_search_records=lambda *args: []),
            _mutations={},
        )

    async def test_report_is_saved_for_loaded_noncurrent_role_and_announced(self):
        from src.agent.daily_report_delivery import run_daily_report

        TodoStore(self.session.storage_root).add("Bring the documents", owner="user")
        calls = []
        async def stream(messages, **kwargs):
            calls.append(messages)
            yield "Your report."

        with patch("src.agent.daily_report_delivery.get_provider", return_value=SimpleNamespace(stream_chat=stream)), \
             patch("src.agent.daily_report_delivery.broadcast.push", new_callable=AsyncMock) as chat, \
             patch("src.agent.daily_report_delivery.broadcast.push_all", new_callable=AsyncMock) as notice:
            self.assertEqual(await run_daily_report(self.runtime, "reporter", "List my to-dos", lambda: True),
                             "completed")
        self.assertEqual(len(calls), 1)
        self.assertIn("Bring the documents", str(calls[0]))
        self.assertEqual(self.session.conversation_store.get_recent()[-1]["content"], "Your report.")
        self.assertEqual(chat.await_args.args[0], "reporter")
        self.assertEqual(notice.await_args.args[0]["type"], "proactive_message")

    async def test_unloaded_role_and_paused_in_flight_report_do_not_publish(self):
        from src.agent.daily_report_delivery import run_daily_report

        with patch("src.agent.daily_report_delivery.get_provider") as get_provider:
            self.assertEqual(await run_daily_report(self.runtime, "unloaded", "Fixture", lambda: True), "skipped")
            get_provider.assert_not_called()
        enabled = True
        async def stream(messages, **kwargs):
            nonlocal enabled
            enabled = False
            yield "Would have been published."

        with patch("src.agent.daily_report_delivery.get_provider", return_value=SimpleNamespace(stream_chat=stream)), \
             patch("src.agent.daily_report_delivery.broadcast.push", new_callable=AsyncMock) as chat:
            self.assertEqual(await run_daily_report(self.runtime, "reporter", "Fixture", lambda: enabled), "skipped")
        chat.assert_not_awaited()
        self.assertEqual(self.session.conversation_store.get_recent(), [])

    async def test_due_schedule_delivers_once_through_the_live_scheduler(self):
        from src.agent.daily_report_delivery import run_daily_report
        from src.agent.daily_reports import DailyReportStore

        now = datetime(2026, 1, 3, 8, 59, 55).timestamp()
        path = Path(self.session.storage_root) / "daily.sqlite3"
        reports = DailyReportStore(path, clock=lambda: now)
        reports.configure("reporter", "09:00", "Fixture", True)
        now += 10
        completed = asyncio.Event()

        async def stream(messages):
            yield "Today's scheduled report."

        async def publish(event):
            completed.set()

        with patch("src.agent.daily_report_delivery.get_provider", return_value=SimpleNamespace(stream_chat=stream)) as model, \
             patch("src.agent.daily_report_delivery.broadcast.push_all", side_effect=publish):
            async with reports.running(partial(run_daily_report, self.runtime)):
                await asyncio.wait_for(completed.wait(), 3)
                for _ in range(10):
                    if reports.get("reporter")["last_status"] == "completed":
                        break
                    await asyncio.sleep(0)
            self.assertEqual(reports.get("reporter")["last_status"], "completed")
            reopened = DailyReportStore(path, clock=lambda: now)
            async with reopened.running(partial(run_daily_report, self.runtime)):
                await asyncio.sleep(0)
            model.assert_called_once()
        self.assertEqual(self.session.conversation_store.get_recent()[-1]["content"], "Today's scheduled report.")

    async def test_provider_error_does_not_save_partial_report(self):
        from src.agent.daily_report_delivery import run_daily_report

        async def stream(messages, **kwargs):
            yield "Incomplete"
            yield "[LLM Error: fixture]"

        with patch("src.agent.daily_report_delivery.get_provider", return_value=SimpleNamespace(stream_chat=stream)):
            with self.assertRaises(RuntimeError):
                await run_daily_report(self.runtime, "reporter", "Fixture", lambda: True)
        self.assertEqual(self.session.conversation_store.get_recent(), [])
