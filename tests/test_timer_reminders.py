"""Offline tests of the dated reminder store, scheduler and timer REST API."""
import asyncio
from datetime import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from src.core.broadcast import BroadcastManager
from src.tools.timer.api import router


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "reminders.sqlite3"
        self.now = 1_800_000_000_000_000

    def store(self):
        from src.tools.timer.reminders import ReminderStore
        return ReminderStore(self.path, clock=lambda: self.now)

    def test_rejects_invalid_and_past_timestamps(self):
        store = self.store()
        for due_at in (None, "2028-01-01", "2028-01-01T19:00", "2028-02-30T19:00:00+08:00",
                       "2028-01-01T19:00:00+08:99", "2028-01-01T19:00:00+24:00", "2020-01-01T19:00:00Z"):
            with self.subTest(due_at=due_at), self.assertRaises(ValueError):
                store.schedule("a", due_at, "bring materials")
        self.assertEqual(store.list_for("a"), [])

    def test_local_time_uses_host_offset_for_the_target_date(self):
        row = self.store().schedule("a", "2028-07-01T19:00:00", "materials")
        self.assertEqual(row["end_timestamp"], datetime(2028, 7, 1, 19).timestamp())
        self.assertIsNotNone(datetime.fromisoformat(row["due_at"]).utcoffset())

    async def test_online_notice_is_scoped_and_dispatched_once_across_restart(self):
        from src.tools.timer.expire_handler import publish_reminder_expiry
        store = self.store()
        due_at = "2027-01-15T17:00:01.1234+08:00"
        row = store.schedule("a", due_at, "bring materials")
        bus = BroadcastManager()
        owner, other_device, other_profile = bus.subscribe("a"), bus.subscribe("a"), bus.subscribe("b")
        with patch("src.tools.timer.expire_handler.broadcast", bus):
            async with store.running(publish_reminder_expiry):
                self.assertEqual(store.list_for("a")[0]["status"], "scheduled")
                await asyncio.sleep(0)
                self.now += 3_602_000_000
                # A host tool runs in a worker thread and wakes the waiting scheduler.
                await asyncio.to_thread(store.schedule, "a", "2028-01-01T19:00:00+08:00", "later")
                event = await asyncio.wait_for(owner.get(), 2)
                self.assertEqual(event, {"type": "timer_expired", "kind": "reminder", "session_id": "a",
                                         "label": "bring materials", "timer_id": row["id"]})
                self.assertEqual(await asyncio.wait_for(other_device.get(), 2), event)
                self.assertEqual(await asyncio.wait_for(other_profile.get(), 2), event)
            reopened = self.store()
            async with reopened.running(publish_reminder_expiry):
                await asyncio.sleep(0)
                saved = reopened.list_for("a")[0]
                self.assertEqual((saved["due_at"], saved["status"]), (due_at, "due"))
                self.assertTrue(owner.empty() and other_device.empty() and other_profile.empty())

    async def test_offline_expiry_survives_restart_and_can_be_dismissed_by_owner(self):
        row = self.store().schedule("a", "2027-01-15T08:00:01Z", "offline")
        self.now += 2_000_000
        reopened = self.store()
        app = FastAPI()
        app.state.agent_runtime = SimpleNamespace(reminders=reopened)
        app.include_router(router)
        events = asyncio.Queue()
        async with reopened.running(events.put):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                for params in ({}, {"session_id": "b"}):
                    self.assertEqual((await http.get("/timers", params=params)).json(), {"timers": []})
                saved = (await http.get("/timers", params={"session_id": "a"})).json()["timers"][0]
                self.assertEqual((saved["id"], saved["status"]), (row["id"], "missed"))
                path = "/timers/" + row["id"]
                foreign_origin = {"Origin": "http://another-site.test"}
                self.assertEqual((await http.get("/timers", params={"session_id": "a"},
                                                 headers=foreign_origin)).status_code, 403)
                self.assertEqual((await http.delete(path, params={"session_id": "a"},
                                                    headers=foreign_origin)).status_code, 403)
                self.assertEqual((await http.delete(path, params={"session_id": "b"})).status_code, 404)
                self.assertEqual((await http.delete(path, params={"session_id": "a"})).status_code, 200)
                self.assertTrue(events.empty())
        self.assertEqual(self.store().list_for("a"), [])

    async def test_cancelled_future_reminder_never_expires(self):
        store = self.store()
        events = asyncio.Queue()
        async with store.running(events.put):
            row = store.schedule("a", "2027-01-15T08:00:01Z", "cancel me")
            self.assertIsNone(store.cancel("b", row["id"]))
            self.assertEqual(store.cancel("a", row["id"])["label"], "cancel me")
            self.now += 2_000_000
            await asyncio.sleep(0)
            self.assertTrue(events.empty())
        self.assertEqual(self.store().list_for("a"), [])
