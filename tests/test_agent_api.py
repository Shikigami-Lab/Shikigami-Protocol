"""Real agent routes with temporary host state; no Docker, model or private data.

Run with Python -B and unittest discovery to avoid repository cache writes.
The chat tests validate the actual request model, not the streaming endpoint.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from fastapi import FastAPI
import httpx
from pydantic import ValidationError
from ruamel.yaml import YAML

from src.agent.link_store import LinkStore
from src.agent.runtime import AgentRuntime
from src.agent.settings import DEFAULTS, normalize_settings
from src.api.agent import router
from src.api.chat import ChatRequest


FAKE_KEY = "synthetic-agent-api-credential-not-a-real-key"
PRESET = {"base_url": "https://fixture.invalid/v1", "model": "fixture", "api_key": FAKE_KEY}
MESSAGES = [{"role": "system", "content": "Synthetic test context"},
            {"role": "user", "content": "Synthetic test request"}]


class FakeSessionManager:
    def __init__(self):
        self.sessions = {name: SimpleNamespace(id=name, profile_id=name)
                         for name in ("alpha", "beta")}
        self.lookups = []

    def get_by_id(self, profile_id):
        self.lookups.append(profile_id)
        return self.sessions.get(profile_id)


class AgentAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shikigami-agent-api-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings_path = self.root / "config" / "app.yaml"
        self.settings_path.parent.mkdir()
        self.settings_path.write_text(
            '# Keep this unrelated comment\n'
            'theme: "synthetic-dark"  # Keep inline comment\n'
            'models:\n  selected: fixture\n  api_key: ' + FAKE_KEY + '\n'
            'unrelated:\n  nested: [one, two]\n'
            'agent:\n  enabled: true\n  max_model_requests: 6\n  timeout_seconds: 120\n',
            encoding="utf-8")
        self.initial_yaml = YAML().load(self.settings_path.read_text(encoding="utf-8"))
        self.config = SimpleNamespace(
            agent={**DEFAULTS, "enabled": True, "api_key": FAKE_KEY, "unknown": "private-setting"},
            get_active_llm_preset=lambda: dict(PRESET))
        self.sm = FakeSessionManager()
        self.app = FastAPI()
        self.app.state.config = self.config
        self.app.state.session_manager = self.sm
        self.app.include_router(router)
        # Block external effects rather than replacing the API/runtime under test.
        self.guards = []
        for target, replacement in (
            ("src.agent.runtime.subprocess.run", Mock(side_effect=AssertionError("No subprocesses"))),
            ("src.agent.runtime.asyncio.create_subprocess_exec", AsyncMock(side_effect=AssertionError("No subprocesses"))),
            ("httpx.AsyncHTTPTransport.handle_async_request", AsyncMock(side_effect=AssertionError("No network"))),
            ("src.core.broadcast.broadcast.push", AsyncMock()),
        ):
            guard = patch(target, replacement)
            guard.start()
            self.addCleanup(guard.stop)
            if "broadcast" not in target:
                self.guards.append(replacement)
        self.timers = Mock(spec=["start_timer", "list_timers", "stop_timer"])
        for name in ("start_timer", "list_timers", "stop_timer"):
            getattr(self.timers, name).side_effect = AssertionError("No timer effects")
        self.runtime = self.make_runtime()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                       base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    def make_runtime(self):
        runtime = AgentRuntime(self.app, root=self.root, timer_manager=self.timers,
                               profile_loader=lambda _: {"tool_configs": {"timer": {"enabled": True}}})
        # Only the external readiness probe is replaced; status() remains real.
        runtime._probe = Mock(return_value={"available": True, "detail": "Synthetic readiness"})
        self.app.state.agent_runtime = runtime
        self.addAsyncCleanup(runtime.close)
        return runtime

    async def asyncTearDown(self):
        await self.runtime.close()
        self.assertEqual(self.timers.mock_calls, [])
        for guard in self.guards:
            guard.assert_not_called()

    def prepare(self, profile="alpha"):
        return self.runtime.prepare(self.sm.sessions[profile], deepcopy(MESSAGES), dict(PRESET))

    async def request(self, method, path, **kwargs):
        response = await self.client.request(method, "/api/agent" + path, **kwargs)
        self.assertNotIn(FAKE_KEY, response.text)
        return response

    async def test_settings_get_returns_only_public_global_fields(self):
        response = await self.request("GET", "/settings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {**DEFAULTS, "enabled": True})
        self.assertEqual(self.runtime._runs, {})

    async def test_disabling_autonomy_cancels_only_reflection_tasks(self):
        self.sm.get_current = lambda: self.sm.sessions['alpha']
        self.config.agent.update(autonomy_enabled=True, workspace_enabled=True)
        self.runtime.profile_loader = lambda p: {'tool_configs': {'agent': {'autonomy_enabled': True}}}
        auto = self.runtime.prepare(self.sm.sessions['alpha'], deepcopy(MESSAGES), dict(PRESET), origin='reflection')
        user = self.prepare('beta')
        response = await self.request('PATCH', '/settings', json={'autonomy_enabled': False})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(auto.stopped.is_set())
        self.assertEqual(self.runtime.store.get_run(auto.id)['state'], 'cancelled')
        self.assertFalse(user.stopped.is_set())

    async def test_autonomy_settings_bounds_and_strict_types(self):
        for body in ({'autonomy_enabled': 1}, {'autonomy_interval_seconds': 59},
                     {'autonomy_max_requests_per_hour': 129}, {'autonomy_max_requests_per_hour': True}):
            response = await self.request('PATCH', '/settings', json=body)
            self.assertEqual(response.status_code, 422)
        response = await self.request('PATCH', '/settings', json={
            'autonomy_enabled': True, 'autonomy_interval_seconds': 60, 'autonomy_max_requests_per_hour': 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['autonomy_max_requests_per_hour'], 1)

    async def test_group_view_blocks_admission_without_changing_task_owner(self):
        self.sm.get_current = lambda: self.sm.sessions['alpha']
        self.config.agent['autonomy_enabled'] = True
        self.runtime.profile_loader = lambda p: {'tool_configs': {'agent': {'autonomy_enabled': True}}}
        auto = self.runtime.prepare(self.sm.sessions['alpha'], deepcopy(MESSAGES), dict(PRESET), origin='reflection')
        response = await self.request('POST', '/view', json={'group_chat': True})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.runtime.autonomy.enabled(self.sm.sessions['alpha']))
        self.runtime._authorize(auto)
        response = await self.request('POST', '/view', json={'group_chat': False})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.runtime.autonomy.enabled(self.sm.sessions['alpha']))

    async def test_per_character_autonomy_revocation_preserves_other_tasks(self):
        import json
        from src.api.tools import router as tools_router
        self.app.include_router(tools_router)
        self.sm.get_current = lambda: self.sm.sessions['alpha']
        self.config.agent['autonomy_enabled'] = True
        self.runtime.profile_loader = lambda p: {'tool_configs': {'agent': {'autonomy_enabled': True}}}
        auto = self.runtime.prepare(self.sm.sessions['alpha'], deepcopy(MESSAGES), dict(PRESET), origin='reflection')
        user = self.prepare('beta')
        folder = self.root / 'fixture-profiles'
        folder.mkdir()
        (folder / 'alpha.json').write_text(json.dumps({'tool_configs': {'agent': {'autonomy_enabled': True}}}))
        with patch('src.api.tools._PROFILES_DIR', str(folder)):
            response = await self.client.patch('/api/profiles/alpha/tool_configs', json={
                'tool_id': 'agent', 'config': {'autonomy_enabled': False}})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(auto.stopped.is_set())
        self.assertFalse(user.stopped.is_set())

    async def test_per_character_tool_master_revokes_only_its_own_task(self):
        import json
        from src.api.tools import router as tools_router
        self.app.include_router(tools_router)
        first, other = self.prepare('alpha'), self.prepare('beta')
        folder = self.root / 'fixture-profiles'
        folder.mkdir()
        (folder / 'alpha.json').write_text(json.dumps({'tool_configs': {'agent': {'enabled': True}}}))
        with patch('src.api.tools._PROFILES_DIR', str(folder)):
            response = await self.client.patch('/api/profiles/alpha/tool_configs', json={
                'tool_id': 'agent', 'config': {'enabled': False}})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(first.stopped.is_set())
        self.assertFalse(other.stopped.is_set())

    async def test_settings_patch_preserves_unrelated_yaml_values_and_comments(self):
        response = await self.request("PATCH", "/settings", json={"max_model_requests": 8})
        self.assertEqual(response.status_code, 200)
        expected = {**DEFAULTS, "enabled": True, "max_model_requests": 8}
        self.assertEqual(response.json(), expected)
        self.assertEqual((await self.request("GET", "/settings")).json(), expected)
        text = self.settings_path.read_text(encoding="utf-8")
        saved = YAML().load(text)
        self.assertEqual(saved["agent"], expected)
        for key in ("theme", "models", "unrelated"):
            self.assertEqual(saved[key], self.initial_yaml[key])
        self.assertIn('# Keep this unrelated comment', text)
        self.assertIn('# Keep inline comment', text)
        self.assertIn('"synthetic-dark"', text)
        self.assertEqual(list(self.settings_path.parent.glob(".agent-settings-*")), [])

    async def test_settings_empty_patch_keeps_values(self):
        response = await self.request("PATCH", "/settings", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {**DEFAULTS, "enabled": True})

    async def test_revoking_code_access_cancels_task_without_disabling_chat_tools(self):
        self.config.agent.update(workspace_enabled=True, code_enabled=True)
        run = self.prepare()
        self.assertTrue(run.access['code_enabled'])
        response = await self.request('PATCH', '/settings', json={'code_enabled': False})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['enabled'])
        self.assertEqual(self.runtime.store.get_run(run.id)['state'], 'cancelled')
        self.assertTrue(run.stopped.is_set())

    async def test_settings_limits_accept_inclusive_bounds(self):
        for requests, timeout in ((1, 10), (32, 600)):
            with self.subTest(requests=requests, timeout=timeout):
                response = await self.request("PATCH", "/settings", json={
                    "max_model_requests": requests, "timeout_seconds": timeout})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["max_model_requests"], requests)
                self.assertEqual(response.json()["timeout_seconds"], timeout)

    async def test_invalid_settings_are_422_without_persisting_or_cancelling(self):
        run = self.prepare()
        before_yaml = self.settings_path.read_bytes()
        before_config = deepcopy(self.config.agent)
        before_run = self.runtime.store.get_run(run.id)
        cases = [{"max_model_requests": value} for value in (0, 33, True, False, 1.0, "6", [], {})]
        cases += [{"timeout_seconds": value} for value in (9, 601, True, False, 10.0, "120", [], {})]
        cases += [{"enabled": value} for value in (0, 1, "false", [], {})]
        cases += [{"extra": True}, {"api_key": "synthetic-extra"}]
        for body in cases:
            with self.subTest(body=body):
                response = await self.request("PATCH", "/settings", json=body)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.settings_path.read_bytes(), before_yaml)
                self.assertEqual(self.config.agent, before_config)
                self.assertEqual(self.runtime.store.get_run(run.id), before_run)

    async def test_explicit_null_settings_rejected_by_normalization_and_route(self):
        before = self.settings_path.read_bytes()
        for field in DEFAULTS:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    normalize_settings({**DEFAULTS, field: None})
                response = await self.request("PATCH", "/settings", json={field: None})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.settings_path.read_bytes(), before)

    async def test_disabling_cancels_active_runs_and_prevents_new_actions(self):
        runs = [self.prepare(profile) for profile in ("alpha", "beta")]
        response = await self.request("PATCH", "/settings", json={"enabled": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enabled"])
        for run in runs:
            self.assertEqual(self.runtime.store.get_run(run.id)["state"], "cancelled")
            with self.assertRaises(ValueError):
                await self.runtime.execute_timer(run.id, "timer_create", {"seconds": 60, "label": "no"}, "late")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.runtime._runs, {})
        self.assertEqual((await self.request("GET", "/status")).json()["state"], "disabled")

    async def test_status_cache_and_explicit_check_use_synthetic_probe(self):
        for _ in range(2):
            response = await self.request("GET", "/status")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["state"], "ready")
            self.assertEqual(response.json()["model"], "fixture")
        self.assertEqual(self.runtime._probe.call_count, 1)
        response = await self.request("POST", "/check")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.runtime._probe.call_count, 2)
        self.assertEqual(self.runtime._runs, {})

    async def test_cross_origin_cannot_read_metadata_or_mutate(self):
        run = self.prepare()
        before_yaml = self.settings_path.read_bytes()
        before_run = self.runtime.store.get_run(run.id)
        routes = [("GET", "/settings", {}), ("GET", "/status", {}),
                  ("GET", "/runs", {"params": {"profile_id": "alpha"}}),
                  ("PATCH", "/settings", {"json": {"enabled": False}}),
                  ("POST", "/check", {}),
                  ("POST", f"/runs/{run.id}/cancel", {"json": {"profile_id": "alpha"}}),
                  ("POST", f"/runs/{run.id}/approval", {"json": {
                      "profile_id": "alpha", "approval_id": "unknown", "outcome": "allowed-once"}})]
        for origin in ("https://foreign.invalid", "https://testserver", "http://testserver:9999", "null"):
            for method, path, kwargs in routes:
                with self.subTest(origin=origin, method=method, path=path):
                    response = await self.request(method, path, headers={"Origin": origin}, **kwargs)
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(self.settings_path.read_bytes(), before_yaml)
        self.assertEqual(self.runtime.store.get_run(run.id), before_run)
        self.assertEqual(self.sm.lookups, [])
        self.runtime._probe.assert_not_called()

    async def test_same_origin_and_no_origin_local_clients_are_allowed(self):
        for headers in ({}, {"Origin": "http://testserver"}):
            with self.subTest(headers=headers):
                response = await self.request("GET", "/settings", headers=headers)
                self.assertEqual(response.status_code, 200)
                response = await self.request("PATCH", "/settings", headers=headers, json={"enabled": True})
                self.assertEqual(response.status_code, 200)

    async def test_runs_maps_native_identity_to_profile_and_projects_events(self):
        run = self.prepare()
        self.prepare("beta")
        for event in (
            {"type": "tool_call", "tool_name": "timer_create", "result": {"arguments": {"seconds": 60}}},
            {"type": "tool_result", "summary": "Synthetic result", "result": {"is_error": True}},
            {"type": "approval_requested", "result": {"approval_id": "approval-1", "reason": "Synthetic reason"}},
        ):
            self.runtime.store.append_event(run.id, event)
        original = deepcopy(self.runtime.store.get_run(run.id))
        self.assertEqual(original["session_id"], run.id)
        response = await self.request("GET", "/runs", params={"profile_id": "alpha"})
        self.assertEqual(response.status_code, 200)
        rows = response.json()["runs"]
        self.assertEqual(len(rows), 1)
        public = rows[0]
        self.assertEqual(public["id"], run.id)
        for item in [public, *public["events"]]:
            self.assertEqual(item["run_id"], run.id)
            self.assertEqual(item["request_id"], run.id)
            self.assertEqual(item["profile_id"], "alpha")
            self.assertEqual(item["session_id"], "alpha")
        for event in public["events"]:
            self.assertEqual(event["sequence"], event["seq"])
        self.assertEqual(public["events"][0]["arguments"], '{"seconds": 60}')
        self.assertTrue(public["events"][1]["is_error"])
        self.assertEqual(public["events"][1]["content"], [{"type": "text", "text": "Synthetic result"}])
        self.assertEqual(public["events"][2]["approval_id"], "approval-1")
        self.assertEqual(self.runtime.store.get_run(run.id), original)

    async def test_invalid_profile_query_is_rejected_before_session_lookup(self):
        for value in ("", ".", "..", "../alpha", "alpha/beta", "alpha\\beta", "C:alpha", "a\x00b", "a" * 257):
            with self.subTest(profile_id=value):
                response = await self.request("GET", "/runs", params={"profile_id": value})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.sm.lookups, [])
        self.assertEqual((await self.request("GET", "/runs")).status_code, 422)
        self.assertEqual(self.sm.lookups, [])

    async def test_unknown_profile_is_404(self):
        response = await self.request("GET", "/runs", params={"profile_id": "missing"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.sm.lookups, ["missing"])

    async def test_foreign_and_missing_runs_cannot_be_cancelled_or_approved(self):
        run = self.prepare()
        before = self.runtime.store.get_run(run.id)
        for run_id, profile in ((run.id, "beta"), (str(uuid.uuid4()), "alpha")):
            for action in ("cancel", "approval"):
                body = {"profile_id": profile}
                if action == "approval":
                    body.update(approval_id="unknown", outcome="allowed-once")
                with self.subTest(run_id=run_id, profile=profile, action=action):
                    response = await self.request("POST", f"/runs/{run_id}/{action}", json=body)
                    self.assertEqual(response.status_code, 404)
        self.assertEqual(self.runtime.store.get_run(run.id), before)
        self.assertFalse(run.stopped.is_set())

    async def test_stale_runs_are_hidden_and_controls_return_404(self):
        run = self.prepare()
        await self.runtime.invalidate("alpha")
        self.assertEqual((await self.request("GET", "/runs", params={"profile_id": "alpha"})).json(), {"runs": []})
        for action, extra in (("cancel", {}), ("approval", {"approval_id": "old", "outcome": "rejected"})):
            response = await self.request("POST", f"/runs/{run.id}/{action}", json={"profile_id": "alpha", **extra})
            self.assertEqual(response.status_code, 404)

    async def test_unknown_and_late_approval_return_false(self):
        run = self.prepare()
        body = {"profile_id": "alpha", "approval_id": "unknown", "outcome": "allowed-once"}
        response = await self.request("POST", f"/runs/{run.id}/approval", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": False})
        await self.runtime._publish(run, {"type": "approval_requested", "approval_id": "pending", "reason": "Synthetic"})
        await self.request("POST", f"/runs/{run.id}/cancel", json={"profile_id": "alpha"})
        response = await self.request("POST", f"/runs/{run.id}/approval", json={**body, "approval_id": "pending"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": False})
        self.assertEqual(self.runtime.store.get_run(run.id)["state"], "cancelled")

    async def test_stopped_pending_approval_returns_false_not_server_error(self):
        run = self.prepare()
        await self.runtime._publish(run, {"type": "approval_requested", "approval_id": "pending", "reason": "Synthetic"})
        self.runtime._mark_stopped(run)
        response = await self.request("POST", f"/runs/{run.id}/approval", json={
            "profile_id": "alpha", "approval_id": "pending", "outcome": "rejected"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": False})

    async def test_control_bodies_reject_extras_nulls_and_invalid_outcomes(self):
        run = self.prepare()
        cases = [("cancel", {}), ("cancel", {"profile_id": None}),
                 ("cancel", {"profile_id": "alpha", "extra": True}),
                 ("approval", {"profile_id": "alpha", "approval_id": None, "outcome": "rejected"}),
                 ("approval", {"profile_id": "alpha", "approval_id": "pending", "outcome": "always"}),
                 ("approval", {"profile_id": "alpha", "approval_id": "pending", "outcome": "rejected", "extra": True})]
        for action, body in cases:
            with self.subTest(action=action, body=body):
                response = await self.request("POST", f"/runs/{run.id}/{action}", json=body)
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.runtime.store.get_run(run.id)["state"], "running")

    async def test_duplicate_cancel_reports_cancelled_without_false_completion(self):
        run = self.prepare()
        for accepted in (True, False):
            response = await self.request("POST", f"/runs/{run.id}/cancel", json={"profile_id": "alpha"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"accepted": accepted, "state": "cancelled"})
        response = await self.request("GET", "/runs", params={"profile_id": "alpha"})
        self.assertEqual(response.status_code, 200)
        row, = response.json()["runs"]
        self.assertEqual(row["state"], "cancelled")
        terminal = [event["type"] for event in row["events"] if event["type"] in ("cancelled", "completed", "failed")]
        self.assertEqual(terminal, ["cancelled"])

    async def test_run_control_profile_pattern_rejects_unsafe_identities(self):
        run = self.prepare()
        before = self.runtime.store.get_run(run.id)
        invalid = ["", "../alpha", "alpha/beta", "alpha\\beta", "C:alpha", "a" * 257]
        invalid += ["a" + chr(code) + "b" for code in (*range(32), *range(127, 160))]
        for profile_id in invalid:
            for action, extra in (("cancel", {}), ("approval", {
                    "approval_id": "pending", "outcome": "rejected"})):
                with self.subTest(profile_id=profile_id, action=action):
                    response = await self.request("POST", f"/runs/{run.id}/{action}",
                                                  json={"profile_id": profile_id, **extra})
                    self.assertEqual(response.status_code, 422)
                    self.assertIn(["body", "profile_id"],
                                  [error["loc"] for error in response.json()["detail"]])
        self.assertEqual(self.runtime.store.get_run(run.id), before)
        self.assertFalse(run.stopped.is_set())

    async def test_cancel_of_completed_run_preserves_real_terminal_outcome(self):
        run = self.prepare()
        await self.runtime._publish(run, {"type": "completed"})
        response = await self.request("POST", f"/runs/{run.id}/cancel", json={"profile_id": "alpha"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": False, "state": "completed"})

    async def test_startup_marks_interrupted_runs_failed_without_resuming_actions(self):
        await self.runtime.close()
        interrupted, completed = str(uuid.uuid4()), str(uuid.uuid4())
        args = {"seconds": 60, "label": "Synthetic pending"}
        store = LinkStore(self.root / "data" / "agent" / "links.sqlite3")
        try:
            store.begin_run(interrupted, "alpha", interrupted)
            self.assertEqual(store.claim_action(interrupted, "pending", "timer_create", args), {"status": "new"})
            store.begin_run(completed, "beta", completed)
            store.finish_run(completed, "completed")
        finally:
            store.close()
        self.runtime = self.make_runtime()
        response = await self.request("GET", "/runs", params={"profile_id": "alpha"})
        self.assertEqual(response.status_code, 200)
        row, = response.json()["runs"]
        self.assertEqual(row["state"], "failed")
        self.assertIn("interrupted", row["error"])
        self.assertEqual(self.runtime.store.claim_action(interrupted, "pending", "timer_create", args), {"status": "pending"})
        self.assertEqual(self.runtime.store.get_run(completed)["state"], "completed")
        self.assertEqual(self.runtime._runs, {})
        observation, = self.runtime.reflection_results("alpha")
        self.assertEqual(observation["payload"], {
            "tool": "agent_task", "result": {"state": "failed", "error": row["error"]}})
        self.assertNotIn("model_requests", observation["payload"]["result"])
        self.assertEqual(self.runtime.reflection_results("beta"), [])
        response = await self.request("POST", f"/runs/{interrupted}/cancel", json={"profile_id": "alpha"})
        self.assertEqual(response.json(), {"accepted": False, "state": "failed"})

    async def test_pending_completed_marker_recovers_as_public_failure_without_replay(self):
        run_id = str(uuid.uuid4())
        args = {"seconds": 60, "label": "Synthetic interrupted action"}
        # Seed the durable crash boundary, with no in-memory worker to cancel
        # during close: append_event committed, finish_run did not commit.
        self.runtime.store.begin_run(run_id, "alpha", run_id)
        self.assertEqual(self.runtime.store.claim_action(
            run_id, "pending", "timer_create", args), {"status": "new"})
        marker = self.runtime.store.append_event(run_id, {
            "type": "status", "status": "running",
            "result": {"terminal_type": "completed", "model_requests": 3}})
        response = await self.request("GET", "/runs", params={"profile_id": "alpha"})
        self.assertEqual(response.status_code, 200)
        before, = response.json()["runs"]
        self.assertEqual(before["state"], "running")
        self.assertEqual([event["type"] for event in before["events"]], ["status"])
        self.assertEqual(self.runtime.reflection_results("alpha"), [])

        observations = None
        for restart in range(2):
            with self.subTest(restart=restart):
                await self.runtime.close()
                self.runtime = self.make_runtime()
                response = await self.request("GET", "/runs", params={"profile_id": "alpha"})
                self.assertEqual(response.status_code, 200)
                row, = response.json()["runs"]
                self.assertEqual(row["state"], "failed")
                self.assertIn("interrupted", row["error"])
                self.assertEqual([event["type"] for event in row["events"]], ["failed"])
                event, = row["events"]
                self.assertEqual(event["error"], row["error"])
                self.assertEqual(event["sequence"], marker["seq"])
                self.assertEqual(event["session_id"], "alpha")
                # The historical marker remains unchanged; public type comes
                # from the authoritative SQL state, never terminal_type.
                self.assertEqual(self.runtime.store.get_run(run_id)["events"], [marker])
                current = self.runtime.reflection_results("alpha")
                observation, = current
                self.assertEqual(observation["payload"], {
                    "tool": "agent_task", "result": {"state": "failed", "error": row["error"]}})
                self.assertNotIn("model_requests", observation["payload"]["result"])
                if observations is not None:
                    self.assertEqual(current, observations, "Restart duplicated the failure observation")
                observations = current
                self.assertEqual(self.runtime.store.claim_action(
                    run_id, "pending", "timer_create", args), {"status": "pending"})
                self.assertEqual(self.runtime._runs, {})


class ChatRequestIdentityTests(unittest.TestCase):
    def test_missing_session_id_preserves_legacy_current_session_default(self):
        body = ChatRequest(message="Synthetic")
        self.assertEqual(body.session_id, "")
        self.assertEqual(body.request_id, "")

    def test_empty_session_id_preserves_legacy_current_session_sentinel(self):
        body = ChatRequest(message="Synthetic", session_id="")
        self.assertEqual(body.session_id, "")

    def test_safe_session_ids_and_request_id_limit_are_accepted(self):
        for session_id in ("alpha", "character_1-test", "角色", "a" * 256):
            with self.subTest(session_id=session_id):
                body = ChatRequest(message="Synthetic", session_id=session_id, request_id="n" * 128)
                self.assertEqual(body.session_id, session_id)
                self.assertEqual(body.request_id, "n" * 128)

    def test_path_separators_colon_and_oversized_session_id_are_rejected(self):
        for value in ("../alpha", "alpha/beta", "alpha\\beta", "C:alpha", "a" * 257, None):
            with self.subTest(session_id=value):
                with self.assertRaises(ValidationError):
                    ChatRequest(message="Synthetic", session_id=value)

    def test_session_id_rejects_ascii_controls(self):
        for code in range(32):
            with self.subTest(code=code):
                with self.assertRaises(ValidationError):
                    ChatRequest(message="Synthetic", session_id="a" + chr(code) + "b")

    def test_session_id_rejects_delete_and_c1_controls(self):
        accepted = []
        for code in range(127, 160):
            try:
                ChatRequest(message="Synthetic", session_id="a" + chr(code) + "b")
            except ValidationError:
                continue
            accepted.append(f"U+{code:04X}")
        self.assertEqual(accepted, [], "Session identity accepted control characters")

    def test_oversized_request_id_is_rejected(self):
        with self.assertRaises(ValidationError):
            ChatRequest(message="Synthetic", session_id="alpha", request_id="n" * 129)


if __name__ == "__main__":
    unittest.main()
