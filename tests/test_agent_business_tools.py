"""Synthetic original-store reads and native MCP business-tool contracts."""
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from src.agent.mcp_server import create_timer_mcp_app
from src.core.session import Session
from src.memory.conversation_store import ConversationStore


@asynccontextmanager
async def business_client(callback, names):
    adapter = create_timer_mcp_app(callback, business_tools=names)
    async with adapter.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=adapter.app)) as http:
            async with streamable_http_client(
                "http://localhost:8000/mcp", http_client=http,
            ) as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    yield client


class BusinessToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from src.agent.business_tools import call_business_tool
        self.call_business_tool = call_business_tool
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = Session("alpha", "alpha", "Alpha", str(Path(self.tmp.name) / "alpha"))
        self.store = self.session.conversation_store
        self.addCleanup(self.store.close)
        self.app = SimpleNamespace()

    def add(self, content, when, role="user"):
        mid = self.store.append(role, content, sender="synthetic")
        with self.store._conn:
            self.store._conn.execute("UPDATE messages SET timestamp=? WHERE id=?", (when, mid))
        return mid

    async def call(self, name="history_search", **args):
        result = await self.call_business_tool(self.app, self.session, name, args)
        json.dumps(result, allow_nan=False)
        return result

    async def test_date_range_includes_both_local_days_with_stable_sources(self):
        start = datetime(2025, 2, 3)
        self.add("needle outside", (start - timedelta(microseconds=1)).timestamp())
        first = self.add("needle start", start.timestamp())
        last = self.add("needle end", (start + timedelta(days=2, microseconds=-1)).timestamp(), "assistant")
        self.add("needle outside", (start + timedelta(days=2)).timestamp())
        self.add("different text", start.timestamp())
        result = await self.call(query="needle", start_date="2025-02-03", end_date="2025-02-04")
        self.assertTrue(result["ok"])
        self.assertEqual([m["id"] for m in result["messages"]], [last, first])
        self.assertEqual(result["messages"][0]["role"], "assistant")
        self.assertEqual(result["messages"][0]["sender"], "synthetic")
        self.assertEqual(result["messages"][1]["timestamp"], start.timestamp())
        self.assertEqual(result["source"], "conversation_store")

    async def test_native_mcp_to_original_history_end_to_end(self):
        mid = self.add("synthetic original quote", datetime(2025, 6, 1, 12).timestamp())

        async def bound_callback(name, args, call_id):
            return await self.call_business_tool(self.app, self.session, name, args)

        async with business_client(bound_callback, ("history_search", "history_context")) as client:
            result = await client.call_tool("history_search", {
                "query": "original quote", "start_date": "2025-06-01", "end_date": "2025-06-01",
            })
            self.assertFalse(result.isError)
            self.assertEqual(result.structuredContent["messages"][0]["id"], mid)
            context = await client.call_tool("history_context", {"message_id": mid, "before": 0, "after": 0})
            self.assertEqual(context.structuredContent["messages"], result.structuredContent["messages"])
        self.assertEqual(self.store.count_all(), 1)

    async def test_literal_query_and_one_sided_dates(self):
        self.add("100%_literal", datetime(2025, 1, 1).timestamp())
        self.add("100-other", datetime(2025, 1, 2).timestamp())
        result = await self.call(query="%_", end_date="2025-01-01")
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual((await self.call(start_date="2025-01-02"))["messages"][0]["content"], "100-other")
        self.assertEqual((await self.call(query="' OR 1=1 --"))["messages"], [])

    async def test_strict_invalid_inputs_do_not_read_history(self):
        cases = [
            {"start_date": "2025-1-01"}, {"start_date": "20250101"},
            {"start_date": "2025-02-29"}, {"end_date": "2025-01-01T00:00:00Z"},
            {"start_date": "2025-02-02", "end_date": "2025-02-01"},
            {"end_date": "9999-12-31"}, {"query": None}, {"query": "x" * 501},
            {"limit": True}, {"limit": "5"}, {"limit": 0}, {"limit": 21},
            {"limit": 1.0}, {"profile_id": "other"}, {"end_date": None},
        ]
        for args in cases:
            with self.subTest(args=args):
                result = await self.call(**args)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "invalid_arguments")

    async def test_context_is_chronological_with_tie_breaking_and_deleted_id(self):
        ids = [self.add(str(i), 1000, "assistant" if i % 2 else "user") for i in range(8)]
        self.store.delete_by_id(ids[1])
        result = await self.call("history_context", message_id=ids[4], before=3, after=2)
        self.assertEqual([m["id"] for m in result["messages"]], [ids[i] for i in (0, 2, 3, 4, 5, 6)])
        self.assertEqual(result["message_id"], ids[4])
        self.assertEqual((await self.call("history_context", message_id=ids[1]))["error"]["code"], "not_found")
        for args in ({"message_id": True}, {"message_id": "4"}, {"message_id": 0},
                     {"message_id": 4, "before": -1}, {"message_id": 4, "after": 11}):
            self.assertEqual((await self.call("history_context", **args))["error"]["code"], "invalid_arguments")

    async def test_bounds_truncation_and_original_incomplete_metadata(self):
        with self.store._conn:
            self.store._conn.execute("ALTER TABLE messages ADD COLUMN incomplete INTEGER")
            self.store._conn.execute("ALTER TABLE messages ADD COLUMN metadata TEXT")
        for i in range(23):
            mid = self.add("x" * 5000, 1000 + i)
            with self.store._conn:
                self.store._conn.execute(
                    "UPDATE messages SET incomplete=1, metadata=? WHERE id=?",
                    (json.dumps({"incomplete": True, "run_id": "prior", "reason": "cancelled"}), mid),
                )
        result = await self.call(limit=20)
        self.assertEqual(len(result["messages"]), 20)
        self.assertTrue(result["has_more"])
        for message in result["messages"]:
            self.assertEqual(len(message["content"]), 4000)
            self.assertTrue(message["content_truncated"])
            self.assertEqual(message["incomplete"], 1)
            self.assertTrue(message["metadata"]["incomplete"])
        self.assertEqual(self.store.count_all(), 23)

    async def test_generation_provenance_and_single_task_message_reader(self):
        from src.agent import business_tools
        self.assertTrue(hasattr(business_tools, "read_task_message"))
        for status in ("completed", "cancelled", "failed"):
            mid = self.add("goal " + "x" * 5000, 1000)
            with self.store._conn:
                self.store._conn.execute(
                    "UPDATE messages SET generation_status=?, agent_run_id=? WHERE id=?",
                    (status, "synthetic-run", mid),
                )
            row = business_tools.read_task_message(self.session, mid)
            self.assertEqual(row["generation_status"], status)
            self.assertEqual(row["agent_run_id"], "synthetic-run")
            self.assertTrue(row["content_truncated"])
            self.assertEqual(len(row["content"]), 4000)
            context = await self.call("history_context", message_id=mid, before=0, after=0)
            self.assertEqual(context["messages"], [row])
        search = await self.call()
        self.assertEqual([row["generation_status"] for row in search["messages"]],
                         ["failed", "cancelled", "completed"])
        self.assertIsNone(business_tools.read_task_message(self.session, mid + 1))
        for invalid in (True, "1", 0, -1):
            with self.assertRaises(ValueError):
                business_tools.read_task_message(self.session, invalid)
        self.session.id = "another-session"
        with self.assertRaises(ValueError):
            business_tools.read_task_message(self.session, mid)

    async def test_task_message_reader_never_creates_missing_store(self):
        from src.agent import business_tools
        self.assertTrue(hasattr(business_tools, "read_task_message"))
        missing = Path(self.tmp.name) / "no-store"
        session = Session("missing", "missing", "Missing", str(missing))
        with self.assertRaises(sqlite3.OperationalError):
            business_tools.read_task_message(session, 1)
        self.assertFalse(missing.exists())
        self.assertIsNone(session._store)

    async def test_late_search_match_is_returned_with_original_source_and_offset(self):
        original = "x" * 6000 + "Unique NEEDLE evidence" + "y" * 6000
        mid = self.add(original, 1000)
        result = await self.call(query="unique needle")
        row = result["messages"][0]
        self.assertEqual(row["id"], mid)
        self.assertIn("Unique NEEDLE evidence", row["content"])
        self.assertEqual(row["content"], original[row["content_offset"]:row["content_offset"] + 4000])
        self.assertEqual(row["content_length"], len(original))
        self.assertTrue(row["content_truncated"])
        self.assertEqual(self.store.get_all_with_id()[0]["content"], original)

    async def test_oversized_metadata_preserves_status_and_marks_omission(self):
        with self.store._conn:
            self.store._conn.execute("ALTER TABLE messages ADD COLUMN metadata TEXT")
        mid = self.add("original", 1000)
        metadata = {"padding": "x" * 5000, "incomplete": True, "agent_run_id": "original-run",
                    "generation_status": "cancelled"}
        with self.store._conn:
            self.store._conn.execute("UPDATE messages SET metadata=?,sender=? WHERE id=?",
                                     (json.dumps(metadata), "s" * 300, mid))
        row = (await self.call())["messages"][0]
        self.assertIsInstance(row["metadata"], dict)
        for key in ("incomplete", "agent_run_id", "generation_status"):
            self.assertEqual(row["metadata"][key], metadata[key])
        self.assertTrue(row["metadata_truncated"])
        self.assertTrue(row["sender_truncated"])
        self.assertEqual(len(row["sender"]), 256)
        # If even the bounded metadata read is insufficient, say unknown, not complete.
        with self.store._conn:
            self.store._conn.execute("UPDATE messages SET metadata=? WHERE id=?",
                                     (json.dumps({"padding": "x" * 100000, "incomplete": True}), mid))
        row = (await self.call())["messages"][0]
        self.assertIsNone(row["metadata"])
        self.assertTrue(row["metadata_unavailable"])
        self.assertTrue(row["metadata_truncated"])

    async def test_task_goal_guard_rejects_wrong_run_role_and_partial_original(self):
        from src.agent.business_tools import read_task_message
        mid = self.store.append("user", "original goal", agent_run_id="bound-run")
        self.assertEqual(read_task_message(self.session, mid, expected_run_id="bound-run")["content"], "original goal")
        with self.assertRaises(ValueError):
            read_task_message(self.session, mid, expected_run_id="other-run")
        for role, content, status in (("assistant", "reply", "completed"),
                                      ("user", "incomplete", "failed"),
                                      ("user", "x" * 4001, "completed")):
            bad = self.store.append(role, content, agent_run_id="bound-run", generation_status=status)
            with self.assertRaises(ValueError):
                read_task_message(self.session, bad, expected_run_id="bound-run")
        # Same integer ID in another profile must not resolve to the original run.
        other = Session("beta", "beta", "Beta", str(Path(self.tmp.name) / "beta"))
        other_store = other.conversation_store
        self.addCleanup(other_store.close)
        other_id = other_store.append("user", "other goal", agent_run_id="other-run")
        self.assertEqual(other_id, mid)
        with self.assertRaises(ValueError):
            read_task_message(other, mid, expected_run_id="bound-run")

    async def test_overlong_urls_are_not_returned_as_altered_citation_targets(self):
        original = "https://example.test/?key=" + "x" * 3000

        async def search(query, max_results=5, *, raise_on_error=False):
            return [{"title": "source", "snippet": "evidence", "url": original}]

        with patch("src.tools.websearch.searcher.search", new=search):
            row = (await self.call("web_search", query="synthetic"))["results"][0]
        self.assertEqual(row["url"], "")
        self.assertTrue(row["url_truncated"])
        self.assertFalse(row["citation_available"])
        self.assertTrue(original.startswith(row["url_excerpt"]))

    async def test_profile_isolation_read_only_and_no_lazy_initialization(self):
        self.add("alpha", 1000)
        other = ConversationStore(str(Path(self.tmp.name) / "beta"))
        try:
            other.append("user", "private beta")
            self.store.reset_context()
            before = self.store._conn.total_changes
            result = await self.call()
            self.assertEqual([m["content"] for m in result["messages"]], ["alpha"])
            self.assertEqual(self.store._conn.total_changes, before)
            self.assertEqual(self.store.count_all(), 1)
        finally:
            other.close()
        missing = Path(self.tmp.name) / "never-created"
        session = Session("missing", "missing", "Missing", str(missing))
        result = await self.call_business_tool(self.app, session, "history_search", {})
        self.assertFalse(result["ok"])
        self.assertFalse(missing.exists())
        self.assertIsNone(session._store)

    async def test_existing_uncached_store_is_read_without_initialization(self):
        self.add("persisted quote", 1000)
        uncached = Session("alpha", "alpha", "Alpha", self.session.storage_root)
        with patch.object(ConversationStore, "__init__", side_effect=AssertionError("No store creation")):
            result = await self.call_business_tool(self.app, uncached, "history_search", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["messages"][0]["content"], "persisted quote")
        self.assertIsNone(uncached._store)

    async def test_history_uses_sqlite_read_only_connection(self):
        self.add("original", 1000)
        original_connect = sqlite3.connect
        connections = []

        def connect(*args, **kwargs):
            connections.append((args, kwargs))
            conn = original_connect(*args, **kwargs)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM messages")
            conn.rollback()  # The rejected test write itself opened a transaction.
            return conn

        with patch("src.agent.business_tools.sqlite3.connect", side_effect=connect):
            self.assertTrue((await self.call())["ok"])
        self.assertTrue(connections)
        self.assertEqual(self.store.count_all(), 1)

    async def test_unknown_tools_and_unbound_sessions_fail_closed(self):
        for name in ("fetch", "bash", "fs", "task_context"):
            self.assertEqual((await self.call(name))["error"]["code"], "unknown_tool")
        self.session.id = "group"
        self.assertEqual((await self.call())["error"]["code"], "invalid_session")

    async def test_search_distinguishes_success_empty_and_exception_and_bounds_fields(self):
        async def search(query, max_results=5, *, raise_on_error=False):
            self.assertTrue(raise_on_error)
            self.assertEqual(max_results, 5)
            if query == "fail":
                raise RuntimeError("SECRET provider details")
            if query == "empty":
                return []
            return [{"title": "t" * 400, "snippet": "s" * 5000, "url": "https://example.test"}] * 8

        with patch("src.tools.websearch.searcher.search", new=search):
            empty = await self.call("web_search", query="empty")
            self.assertTrue(empty["ok"])
            self.assertEqual(empty["results"], [])
            result = await self.call("web_search", query="result")
            self.assertEqual(len(result["results"]), 5)
            self.assertLessEqual(len(result["results"][0]["snippet"]), 2000)
            failed = await self.call("web_search", query="fail")
            self.assertEqual(failed["error"]["code"], "search_failed")
            self.assertNotIn("SECRET", json.dumps(failed))
            for value in ("", " ", None, 3, "x" * 501):
                self.assertEqual((await self.call("web_search", query=value))["error"]["code"], "invalid_arguments")

    async def test_legacy_silent_searcher_is_not_misreported_as_empty_success(self):
        async def legacy(query, max_results=5):
            self.fail("Silent legacy API must not be called")
        with patch("src.tools.websearch.searcher.search", new=legacy):
            self.assertEqual((await self.call("web_search", query="test"))["error"]["code"], "search_unavailable")


class SearchProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_does_not_start_a_new_provider_after_inflight_failure(self):
        import asyncio
        import threading
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        client = self.ddg()
        def call(*args, **kwargs):
            if kwargs['backend'] == 'duckduckgo':
                entered.set()
                release.wait(2)
                returned.set()
                raise RuntimeError('provider failed after cancellation')
            return []
        client.text.side_effect = call
        task = asyncio.create_task(self.searcher.search('query', raise_on_error=True))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        finally:
            release.set()
        self.assertTrue(await asyncio.to_thread(returned.wait, 1))
        await asyncio.sleep(.05)
        self.assertEqual([c.kwargs['backend'] for c in client.text.call_args_list], ['duckduckgo'])

    async def test_ddg_failure_uses_one_yahoo_fallback(self):
        client = self.ddg()
        client.text.side_effect = [RuntimeError('No results found'),
            [{'title': 'Result', 'href': 'https://example.test/news', 'body': 'source text'}]]
        result = await self.searcher.search('query', raise_on_error=True)
        self.assertEqual(result[0]['url'], 'https://example.test/news')
        self.assertEqual([c.kwargs['backend'] for c in client.text.call_args_list], ['duckduckgo', 'yahoo'])

    async def test_all_free_sources_fail_without_unbounded_retry(self):
        client = self.ddg(error=RuntimeError('private proxy detail'))
        with self.assertRaises(self.searcher.SearchError):
            await self.searcher.search('query', raise_on_error=True)
        self.assertEqual([c.kwargs['backend'] for c in client.text.call_args_list], ['duckduckgo', 'yahoo'])

    async def test_empty_ddg_result_is_not_an_error_and_does_not_trigger_retry(self):
        client = self.ddg([])
        self.assertEqual(await self.searcher.search('query', raise_on_error=True), [])
        self.assertEqual(client.text.call_count, 1)

    async def test_disabled_fallback_never_silently_selects_auto(self):
        self.engine_registry['text'].pop('yahoo')
        client = self.ddg(error=RuntimeError('No results found'))
        with self.assertRaises(self.searcher.SearchError):
            await self.searcher.search('query', raise_on_error=True)
        self.assertEqual([c.kwargs['backend'] for c in client.text.call_args_list], ['duckduckgo'])

    async def test_only_registered_sources_are_called(self):
        self.engine_registry['text'].pop('duckduckgo')
        client = self.ddg([{'title': 'Result', 'href': 'https://example.test', 'body': 'text'}])
        self.assertTrue(await self.searcher.search('query', raise_on_error=True))
        self.assertEqual([c.kwargs['backend'] for c in client.text.call_args_list], ['yahoo'])

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        from src.tools.websearch import searcher
        self.searcher = searcher
        for name in ("log_tool_websearch_request", "log_tool_websearch_result"):
            stack.enter_context(patch.object(searcher, name))
        stack.enter_context(patch.dict("os.environ", {"WEBSEARCH_SERPER_API_KEY": ""}))
        # Any provider use must go through synthetic transports below.
        self.http_factory = stack.enter_context(patch.object(searcher.httpx, "AsyncClient"))
        self.http_factory.side_effect = AssertionError("No real HTTP requests")
        self.ddg_factory = MagicMock(side_effect=AssertionError("No real DDG requests"))
        self.engine_registry = {"text": {"duckduckgo": object(), "yahoo": object()}}
        stack.enter_context(patch.dict(sys.modules, {"ddgs": SimpleNamespace(DDGS=self.ddg_factory),
            "ddgs.engines": SimpleNamespace(ENGINES=self.engine_registry)}))

    def serper(self, payload=None, error=None):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        response = MagicMock()
        response.json.return_value = payload
        client.post = AsyncMock(return_value=response, side_effect=error)
        self.http_factory.side_effect = None
        self.http_factory.return_value = client
        return client

    def ddg(self, rows=None, error=None):
        client = MagicMock()
        client.__enter__.return_value = client
        client.text.return_value = rows if rows is not None else []
        client.text.side_effect = error
        self.ddg_factory.side_effect = None
        self.ddg_factory.return_value = client
        return client

    async def test_ddg_proxy_precedence_and_explicit_backend(self):
        cases = [
            ({"DDGS_PROXY": "http://dedicated.invalid:8001", "HTTPS_PROXY": "http://secure.invalid:8002",
              "HTTP_PROXY": "http://plain.invalid:8003"}, "http://dedicated.invalid:8001"),
            ({"HTTPS_PROXY": "http://secure.invalid:8002", "HTTP_PROXY": "http://plain.invalid:8003"},
             "http://secure.invalid:8002"),
            ({"HTTP_PROXY": "http://plain.invalid:8003"}, "http://plain.invalid:8003"),
            ({"DDGS_PROXY": "  ", "HTTPS_PROXY": " http://secure.invalid:8002 "}, "http://secure.invalid:8002"),
            ({"https_proxy": "http://secure.invalid:8002", "http_proxy": "http://plain.invalid:8003"},
             "http://secure.invalid:8002"),
            ({"http_proxy": "http://plain.invalid:8003"}, "http://plain.invalid:8003"),
            ({}, None),
        ]
        for environment, expected in cases:
            with self.subTest(environment=environment), patch.dict("os.environ", environment, clear=True):
                self.ddg_factory.reset_mock()
                client = self.ddg([])
                self.assertEqual(await self.searcher._search_ddg("synthetic", 3, raise_on_error=True), [])
                self.ddg_factory.assert_called_once_with(proxy=expected, timeout=self.searcher._TIMEOUT)
                client.text.assert_called_once_with("synthetic", max_results=3, backend="duckduckgo")

    async def test_ddg_proxy_credentials_do_not_enter_logs_or_exception_trace(self):
        import traceback
        proxy = "http://synthetic-user:synthetic-password@proxy.invalid:8080"
        error = RuntimeError("Connection failed for " + proxy + " password=synthetic-password")
        for constructor_failure in (False, True):
            with self.subTest(constructor_failure=constructor_failure), \
                    patch.dict("os.environ", {"DDGS_PROXY": proxy}, clear=True):
                self.ddg(error=error)
                if constructor_failure:
                    self.ddg_factory.side_effect = error
                self.searcher.log_tool_websearch_result.reset_mock()
                with self.assertLogs(self.searcher.logger, level="WARNING") as logs:
                    try:
                        await self.searcher._search_ddg("public query", 3, raise_on_error=True)
                    except self.searcher.SearchError as caught:
                        rendered_trace = "".join(traceback.format_exception(type(caught), caught, caught.__traceback__))
                    else:
                        self.fail("Provider failure must raise SearchError")
                output = "\n".join(logs.output) + repr(self.searcher.log_tool_websearch_result.call_args_list) + rendered_trace
                for secret in (proxy, "synthetic-user", "synthetic-password"):
                    self.assertNotIn(secret, output)
                self.assertIn("RuntimeError", output)

    async def test_serper_strict_zero_is_success_without_fallback(self):
        self.serper({"organic": []})
        with patch.dict("os.environ", {"WEBSEARCH_SERPER_API_KEY": "synthetic-key"}):
            self.assertEqual(await self.searcher.search("zero", raise_on_error=True), [])
        self.ddg_factory.assert_not_called()

    async def test_legacy_empty_serper_still_falls_back(self):
        self.serper({"organic": []})
        self.ddg([{"title": "DDG", "body": "evidence", "href": "https://example.test/source"}])
        with patch.dict("os.environ", {"WEBSEARCH_SERPER_API_KEY": "synthetic-key"}):
            result = await self.searcher.search("query")
        self.assertEqual(result, [{"title": "DDG", "snippet": "evidence", "url": "https://example.test/source"}])

    async def test_serper_failure_falls_back_and_final_failure_raises_only_when_opted_in(self):
        self.serper(error=httpx.ConnectError("synthetic secret transport detail"))
        with patch.dict("os.environ", {"WEBSEARCH_SERPER_API_KEY": "synthetic-key"}):
            self.ddg([{"title": "fallback", "body": "ok", "href": "https://example.test"}])
            self.assertEqual((await self.searcher.search("query", raise_on_error=True))[0]["title"], "fallback")
            self.ddg([])
            self.assertEqual(await self.searcher.search("query", raise_on_error=True), [])
            self.ddg(error=RuntimeError("synthetic secret DDG details"))
            with self.assertRaises(RuntimeError) as raised:
                await self.searcher.search("query", raise_on_error=True)
            self.assertNotIn("secret", str(raised.exception))
            self.assertEqual(await self.searcher.search("query"), [])

    async def test_direct_provider_errors_and_missing_package_respect_flag(self):
        self.serper(error=httpx.ConnectError("synthetic failure"))
        self.assertEqual(await self.searcher._search_serper("q", "synthetic", 5), [])
        with self.assertRaises(RuntimeError):
            await self.searcher._search_serper("q", "synthetic", 5, raise_on_error=True)
        self.ddg(error=RuntimeError("synthetic failure"))
        self.assertEqual(await self.searcher._search_ddg("q", 5), [])
        with self.assertRaises(RuntimeError):
            await self.searcher._search_ddg("q", 5, raise_on_error=True)
        with patch.dict(sys.modules, {"ddgs": None}):
            self.assertEqual(await self.searcher.search("query"), [])
            with self.assertRaises(RuntimeError):
                await self.searcher.search("query", raise_on_error=True)

    async def test_normalized_sources_and_empty_ddg_are_preserved(self):
        self.serper({"organic": [{"title": "Serper", "snippet": "evidence", "link": "https://example.test/s"}]})
        with patch.dict("os.environ", {"WEBSEARCH_SERPER_API_KEY": "synthetic-key"}):
            result = await self.searcher.search("query", raise_on_error=True)
        self.assertEqual(result, [{"title": "Serper", "snippet": "evidence", "url": "https://example.test/s"}])
        self.ddg([])
        self.assertEqual(await self.searcher.search("query", raise_on_error=True), [])

    async def test_http_and_malformed_provider_responses_are_failures(self):
        client = self.serper({"organic": []})
        response = client.post.return_value
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "synthetic rejected request", request=httpx.Request("POST", "https://example.test"),
            response=httpx.Response(429),
        )
        with self.assertRaises(RuntimeError):
            await self.searcher._search_serper("q", "synthetic", 5, raise_on_error=True)
        for payload in (None, {"organic": None}, {"organic": [None]}, {"error": "bad credentials"}):
            self.serper(payload)
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                await self.searcher._search_serper("q", "synthetic", 5, raise_on_error=True)
        self.ddg([None])
        with self.assertRaises(RuntimeError):
            await self.searcher._search_ddg("q", 5, raise_on_error=True)

    async def test_ddg_filtered_invalid_rows_are_not_successful_zero_results(self):
        self.ddg([{"error": "synthetic provider error"}])
        self.assertEqual(await self.searcher.search("query"), [])
        with self.assertRaises(RuntimeError):
            await self.searcher.search("query", raise_on_error=True)

    async def test_real_dispatcher_maps_provider_failure_and_zero_distinctly(self):
        from src.agent.business_tools import call_business_tool
        session = SimpleNamespace(id="synthetic", profile_id="synthetic")
        self.ddg([])
        empty = await call_business_tool(None, session, "web_search", {"query": "zero"})
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["results"], [])
        self.ddg(error=RuntimeError("synthetic secret transport details"))
        failed = await call_business_tool(None, session, "web_search", {"query": "failure"})
        self.assertEqual(failed["error"]["code"], "search_failed")
        self.assertNotIn("secret", json.dumps(failed))


class BusinessMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_tools_only_and_strict_typed_schemas(self):
        callback = AsyncMock(return_value={"ok": True})
        names = ("history_search", "history_context", "web_search", "task_context")
        async with business_client(callback, names) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertEqual(set(tools), set(names) | {"timer_create", "timer_list", "timer_cancel"})
            for name in names:
                self.assertFalse(tools[name].inputSchema["additionalProperties"])
                self.assertTrue(tools[name].annotations.readOnlyHint)
            self.assertTrue(tools["web_search"].annotations.openWorldHint)
            self.assertEqual(tools["task_context"].inputSchema["properties"]["run_id"]["default"], "")
            cases = [("history_search", {}), ("history_context", {"message_id": 12}),
                     ("web_search", {"query": "synthetic"}), ("task_context", {}),
                     ("task_context", {"run_id": "prior"})]
            for name, args in cases:
                self.assertFalse((await client.call_tool(name, args)).isError)
            self.assertEqual(callback.await_args_list[0].args[:2],
                             ("history_search", {"query": "", "start_date": "", "end_date": "", "limit": 5}))
            self.assertEqual(callback.await_args_list[1].args[:2],
                             ("history_context", {"message_id": 12, "before": 3, "after": 3}))
            self.assertEqual(callback.await_args_list[3].args[:2], ("task_context", {"run_id": ""}))
            self.assertTrue(all(isinstance(c.args[2], str) for c in callback.await_args_list))
            callback.reset_mock()
            invalid = [("history_search", {"limit": True}), ("history_search", {"limit": "2"}),
                       ("history_search", {"start_date": "2025-1-1"}),
                       ("history_search", {"start_date": "2025-02-29"}),
                       ("history_search", {"start_date": "2025-02-02", "end_date": "2025-02-01"}),
                       ("history_context", {"message_id": "12"}),
                       ("history_context", {"message_id": 12, "after": 11}),
                       ("web_search", {"query": 1}), ("web_search", {"query": " "}),
                       ("task_context", {"run_id": None})]
            for name, args in invalid:
                self.assertTrue((await client.call_tool(name, args)).isError)
            for name, args in cases:
                self.assertTrue((await client.call_tool(name, {**args, "profile_id": "other"})).isError)
            callback.assert_not_awaited()

    async def test_opt_in_and_callback_metadata_preserved(self):
        receipt = {"ok": True, "incomplete": True, "state": "cancelled", "run_id": "prior"}
        callback = AsyncMock(return_value=receipt)
        async with business_client(callback, ("task_context",)) as client:
            self.assertEqual({t.name for t in (await client.list_tools()).tools},
                             {"timer_create", "timer_list", "timer_cancel", "task_context"})
            self.assertTrue((await client.call_tool("history_search", {})).isError)
            result = await client.call_tool("task_context", {})
            self.assertEqual(result.structuredContent, receipt)
        for names in (("fetch",), ("history_search", "history_search"), "history_search"):
            with self.assertRaises(ValueError):
                create_timer_mcp_app(callback, business_tools=names)


if __name__ == "__main__":
    unittest.main()
