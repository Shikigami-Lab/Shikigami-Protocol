"""Official MCP client -> in-process HTTP -> run-bound timer callback tests."""
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from src.agent.mcp_server import create_timer_mcp_app


@asynccontextmanager
async def connected(call_tool):
    """Use native HTTP lifecycle/transport without opening a network listener."""
    adapter = create_timer_mcp_app(call_tool)
    requests = []

    async def record(request):
        if request.method == "POST":
            requests.append(json.loads(request.content))

    async with adapter.lifespan():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=adapter.app),
            event_hooks={"request": [record]},
        ) as http:
            async with streamable_http_client(
                "http://localhost:8000/mcp", http_client=http,
            ) as (read, write, session_id):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    yield client, adapter, requests, session_id


class TimerMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []

    async def call_tool(self, name, args, request_id):
        self.calls.append((name, args, request_id))
        return {"ok": True, "name": name, "args": args}

    async def test_native_discovery_has_only_typed_timer_inputs(self):
        async with connected(self.call_tool) as (client, adapter, _, session_id):
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertEqual(set(tools), {"timer_create", "timer_list", "timer_cancel"})
            self.assertEqual(adapter.mcp.name, "shikigami")
            self.assertIsNone(session_id())
            self.assertTrue(adapter.mcp.settings.stateless_http)
            self.assertTrue(adapter.mcp.settings.json_response)
            self.assertEqual(adapter.mcp.settings.streamable_http_path, "/mcp")
            for tool in tools.values():
                self.assertIs(tool.inputSchema["additionalProperties"], False)
                self.assertIsNotNone(tool.outputSchema)
            create = tools["timer_create"].inputSchema
            self.assertEqual(set(create["properties"]), {"seconds", "label"})
            self.assertEqual(set(create["required"]), {"seconds", "label"})
            seconds = create["properties"]["seconds"]
            self.assertEqual((seconds["type"], seconds["minimum"], seconds["maximum"]),
                             ("integer", 1, 86400))
            self.assertEqual(create["properties"]["label"]["maxLength"], 128)
            self.assertEqual(tools["timer_list"].inputSchema["properties"], {})
            cancel = tools["timer_cancel"].inputSchema
            self.assertEqual(set(cancel["properties"]), {"timer_id"})
            timer_id = cancel["properties"]["timer_id"]
            self.assertEqual((timer_id["type"], timer_id["minLength"], timer_id["maxLength"]),
                             ("string", 1, 128))
        self.assertEqual(self.calls, [])

    async def test_calls_forward_json_and_native_request_ids(self):
        async with connected(self.call_tool) as (client, _, requests, _):
            cases = [("timer_create", {"seconds": 1, "label": ""}),
                     ("timer_create", {"seconds": 86400, "label": "茶" * 128}),
                     ("timer_list", {}), ("timer_cancel", {"timer_id": "x" * 128})]
            for name, args in cases:
                result = await client.call_tool(name, args)
                self.assertFalse(result.isError)
                expected = {"ok": True, "name": name, "args": args}
                self.assertEqual(result.structuredContent, expected)
                self.assertEqual(json.loads(result.content[0].text), expected)
            wire = [request for request in requests if request["method"] == "tools/call"]
            self.assertEqual(self.calls, [(name, args, str(request["id"]))
                                         for (name, args), request in zip(cases, wire)])
            self.assertEqual(len({call[2] for call in self.calls}), len(cases))

    async def test_invalid_values_fail_before_callback(self):
        cases = [("timer_create", {"seconds": value, "label": "tea"})
                 for value in (0, -1, 86401, True, 1.0, 1.5, "10", None)]
        cases += [("timer_create", {"seconds": 10, "label": value})
                  for value in ("x" * 129, 1, None, [])]
        cases += [("timer_cancel", {"timer_id": value})
                  for value in ("", "x" * 129, 1, None, [])]
        cases += [("timer_create", {}), ("timer_create", {"seconds": 1}),
                  ("timer_create", {"label": "tea"}), ("timer_cancel", {})]
        async with connected(self.call_tool) as (client, _, _, _):
            for name, args in cases:
                with self.subTest(name=name, args=args):
                    self.assertTrue((await client.call_tool(name, args)).isError)
        self.assertEqual(self.calls, [])

    async def test_extra_fields_cannot_supply_authority_or_destinations(self):
        async with connected(self.call_tool) as (client, _, _, _):
            for name, args in [("timer_create", {"seconds": 5, "label": "tea"}),
                               ("timer_list", {}), ("timer_cancel", {"timer_id": "x"})]:
                for key in ("profile_id", "session_id", "run_id", "request_id",
                            "url", "path", "ctx", "unexpected"):
                    with self.subTest(name=name, key=key):
                        result = await client.call_tool(name, {**args, key: "untrusted"})
                        self.assertTrue(result.isError)
        self.assertEqual(self.calls, [])

    async def test_unknown_tool_never_reaches_callback(self):
        async with connected(self.call_tool) as (client, _, _, _):
            result = await client.call_tool("read_file", {"path": "anything"})
            self.assertTrue(result.isError)
        self.assertEqual(self.calls, [])

    async def test_service_exception_is_native_tool_error_and_session_survives(self):
        async def denied(name, args, request_id):
            self.calls.append((name, args, request_id))
            raise PermissionError("timer disabled for this run")

        async with connected(denied) as (client, _, _, _):
            result = await client.call_tool("timer_list", {})
            self.assertTrue(result.isError)
            self.assertIn("timer disabled for this run", result.content[0].text)
            self.assertEqual(len((await client.list_tools()).tools), 3)
        self.assertEqual(len(self.calls), 1)

    async def test_service_error_receipt_is_preserved_without_reinterpretation(self):
        receipt = {"ok": False, "error": {"code": "not_found"}, "receipt_id": "r1"}

        async def failed(name, args, request_id):
            return receipt

        async with connected(failed) as (client, _, _, _):
            result = await client.call_tool("timer_cancel", {"timer_id": "missing"})
            self.assertFalse(result.isError)
            self.assertEqual(result.structuredContent, receipt)
            self.assertEqual(json.loads(result.content[0].text), receipt)

    async def test_two_run_adapters_keep_their_bound_callbacks(self):
        async def other_run(name, args, request_id):
            return {"run": "other"}

        async with connected(self.call_tool) as (first, first_adapter, _, _):
            async with connected(other_run) as (second, second_adapter, _, _):
                self.assertIsNot(first_adapter.mcp, second_adapter.mcp)
                self.assertEqual((await second.call_tool("timer_list", {})).structuredContent,
                                 {"run": "other"})
                self.assertEqual(self.calls, [])
                self.assertTrue((await first.call_tool("timer_list", {})).structuredContent["ok"])
        self.assertEqual(len(self.calls), 1)

    async def test_real_existing_manager_create_list_cancel_via_callback(self):
        from src.tools.timer.manager import TimerManager

        manager = TimerManager()
        session = "synthetic-bound-session"

        async def bound_manager(name, args, request_id):
            if name == "timer_create":
                return {"timer_id": manager.start_timer(**args, session_id=session)}
            if name == "timer_list":
                return {"timers": manager.list_timers()}
            return {"cancelled": manager.stop_timer(args["timer_id"]) is not None}

        # Keep this real-manager acceptance check from writing diagnostic logs.
        with patch("src.tools.timer.manager.log_timer"), \
                patch("src.tools.timer.manager.log_tool_timer"):
            try:
                async with connected(bound_manager) as (client, _, _, _):
                    created = await client.call_tool("timer_create", {"seconds": 3600, "label": "tea"})
                    timer_id = created.structuredContent["timer_id"]
                    listed = (await client.call_tool("timer_list", {})).structuredContent["timers"]
                    self.assertEqual(len(listed), 1)
                    self.assertEqual((listed[0]["id"], listed[0]["session_id"]), (timer_id, session))
                    cancelled = await client.call_tool("timer_cancel", {"timer_id": timer_id})
                    self.assertEqual(cancelled.structuredContent, {"cancelled": True})
                    self.assertEqual(manager.list_timers(), [])
            finally:
                for timer in manager.list_timers():
                    manager.stop_timer(timer["id"])


if __name__ == "__main__":
    unittest.main()
