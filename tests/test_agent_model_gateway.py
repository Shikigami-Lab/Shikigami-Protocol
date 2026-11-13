"""Provider/task interoperability with synthetic keys, temporary state and fake egress."""
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import sys

import httpx

from src.agent.model_gateway import ModelGateway, ModelRequestLimitReached, model_route
from src.agent.runtime import AgentRuntime
from src.llm.registry import preset_config_missing


NATIVE = {"type": "gemini", "base_url": "", "api_key": "synthetic-key", "model": "models/gemini-2.5-flash"}
MODEL = "gemini-2.5-flash"
NATIVE_PATH = "/v1beta/models/" + MODEL + ":streamGenerateContent"
MESSAGES = [{"role": "system", "content": "Synthetic context"}, {"role": "user", "content": "Synthetic task"}]


class ModelRouteTests(unittest.TestCase):
    def test_native_default_official_and_proxy_routes(self):
        cases = (
            ("", "https://generativelanguage.googleapis.com"),
            ("https://generativelanguage.googleapis.com/v1beta/openai/", "https://generativelanguage.googleapis.com"),
            ("https://native.fixture", "https://native.fixture"),
            ("https://native.fixture/proxy/", "https://native.fixture/proxy"),
        )
        for base, target in cases:
            with self.subTest(base=base):
                route = model_route({**NATIVE, "base_url": base})
                self.assertEqual(route, {"provider": "google", "model": MODEL, "native_google": True,
                                         "target": target + NATIVE_PATH + "?alt=sse"})

    def test_versioned_native_base_preserves_the_api_version_once(self):
        for base, expected in (
            ("https://generativelanguage.googleapis.com/v1beta", "https://generativelanguage.googleapis.com/v1beta"),
            ("https://native.fixture/prefix/v1beta/", "https://native.fixture/prefix/v1beta"),
            ("https://native.fixture/prefix/v1", "https://native.fixture/prefix/v1"),
        ):
            with self.subTest(base=base):
                route = model_route({**NATIVE, "base_url": base})
                self.assertEqual(route["target"], expected + "/models/" + MODEL + ":streamGenerateContent?alt=sse")

    def test_compatibility_type_stays_compatible_even_on_google_host(self):
        for ptype in (None, "openai_compat", "openai", "deepseek", "ollama", "lmstudio"):
            for base, target in (
                ("https://generativelanguage.googleapis.com/v1beta/openai/", "https://generativelanguage.googleapis.com/v1beta/openai"),
                ("https://generativelanguage.googleapis.com", "https://generativelanguage.googleapis.com/v1beta/openai"),
                ("https://gateway.fixture/openai/chat/completions", "https://gateway.fixture/openai"),
            ):
                preset = {"base_url": base, "model": MODEL}
                if ptype is not None:
                    preset["type"] = ptype
                with self.subTest(ptype=ptype, base=base):
                    self.assertEqual(model_route(preset), {"provider": "shikigami-model", "model": MODEL,
                        "native_google": False, "target": target + "/chat/completions"})

    def test_custom_legacy_gemini_compatibility_urls_fail_with_migration_guidance(self):
        for base in ("https://gateway.fixture/v1beta/openai/", "https://gateway.fixture/openai/chat/completions"):
            with self.subTest(base=base), self.assertRaisesRegex(ValueError, "openai_compat"):
                model_route({**NATIVE, "base_url": base})

    def test_bad_endpoints_are_rejected_without_normalizing_away_authority(self):
        for base in ("file:///tmp/fixture", "https://user:password@native.fixture", "https://user:password@generativelanguage.googleapis.com/v1beta/openai"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                model_route({**NATIVE, "base_url": base})


class ModelGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []
        self.frames = []
        self.authorize = Mock()

        def provider(request):
            self.sent.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b'data: {"text":"fixture"}\n\n')

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        self.addAsyncCleanup(self.client.aclose)

    async def respond(self, stage, **fields):
        self.frames.append((stage, fields))

    def gateway(self, preset=None, max_requests=1):
        return ModelGateway(preset or NATIVE, max_requests, authorize=self.authorize, client=self.client)

    async def test_native_default_and_proxy_egress_use_google_wire_protocol(self):
        for base in ("", "https://native.fixture/proxy"):
            with self.subTest(base=base):
                preset = {**NATIVE, "base_url": base, "max_tokens": 512, "temperature": .2, "top_p": .6}
                gateway = self.gateway(preset)
                body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                        "generationConfig": {"maxOutputTokens": 4096, "temperature": 2, "topP": 1, "topK": 50,
                                             "thinkingConfig": {"thinkingBudget": 32768, "includeThoughts": True}}}
                await gateway.relay({"method": "POST", "path": NATIVE_PATH + "?alt=sse", "body": body}, self.respond)
                sent = self.sent[-1]
                self.assertEqual(str(sent.url), gateway.route["target"])
                self.assertEqual(sent.headers["x-goog-api-key"], NATIVE["api_key"])
                self.assertNotIn("authorization", sent.headers)
                config = json.loads(sent.content)["generationConfig"]
                self.assertEqual(config, {"maxOutputTokens": 512, "temperature": .2, "topP": .6,
                                          "thinkingConfig": {"includeThoughts": False}})
                self.assertGreaterEqual(self.authorize.call_count, 2)

    async def test_sampling_disabled_in_settings_cannot_be_restored_by_worker(self):
        gateway = self.gateway({**NATIVE, "base_url": "https://native.fixture", "unsupported_params": ["temperature", "top_p"]})
        await gateway.relay({"method": "POST", "path": NATIVE_PATH,
                             "body": {"generationConfig": {"temperature": 1, "topP": 1}}}, self.respond)
        self.assertEqual(json.loads(self.sent[-1].content)["generationConfig"], {"maxOutputTokens": 4096})

    async def test_google_compatibility_egress_uses_bearer_and_chat_completions(self):
        gateway = self.gateway({**NATIVE, "type": "openai_compat", "model": MODEL,
                                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"})
        await gateway.relay({"method": "POST", "path": "/v1/chat/completions",
                             "body": {"messages": MESSAGES, "stream": True}}, self.respond)
        sent = self.sent[-1]
        self.assertEqual(str(sent.url), "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
        self.assertEqual(sent.headers["authorization"], "Bearer " + NATIVE["api_key"])
        self.assertNotIn("x-goog-api-key", sent.headers)

    async def test_worker_endpoint_and_request_count_are_still_bound(self):
        gateway = self.gateway({**NATIVE, "base_url": "https://native.fixture"})
        for method, path in (("GET", NATIVE_PATH), ("POST", "/v1/chat/completions"),
                             ("POST", "/v1beta/models/other:streamGenerateContent")):
            with self.subTest(method=method, path=path), self.assertRaisesRegex(ValueError, "Unapproved"):
                await gateway.relay({"method": method, "path": path, "body": {}}, self.respond)
        self.assertEqual(gateway.requests, 0)
        self.assertEqual(self.sent, [])
        await gateway.relay({"method": "POST", "path": NATIVE_PATH, "body": {}}, self.respond)
        with self.assertRaises(ModelRequestLimitReached):
            await gateway.relay({"method": "POST", "path": NATIVE_PATH, "body": {}}, self.respond)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(gateway.limit_reached)

    async def test_revoked_authority_prevents_native_egress(self):
        gateway = self.gateway({**NATIVE, "base_url": "https://native.fixture"})
        self.authorize.side_effect = ValueError("Task stopped")
        with self.assertRaisesRegex(ValueError, "Task stopped"):
            await gateway.relay({"method": "POST", "path": NATIVE_PATH, "body": {}}, self.respond)
        self.assertEqual(self.sent, [])

    async def test_native_token_budget_is_positive_and_capped(self):
        for requested, expected in ((64, 64), (8192, 256), (-1, 256), (True, 256), ("bad", 256)):
            with self.subTest(requested=requested):
                gateway = self.gateway({**NATIVE, "base_url": "https://native.fixture", "max_tokens": 256})
                await gateway.relay({"method": "POST", "path": NATIVE_PATH,
                    "body": {"generationConfig": {"maxOutputTokens": requested}}}, self.respond)
                self.assertEqual(json.loads(self.sent[-1].content)["generationConfig"]["maxOutputTokens"], expected)


class NativeTaskPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_blank_native_endpoint_selects_and_prepares_task(self):
        with tempfile.TemporaryDirectory() as root, \
                patch("src.agent.runtime.subprocess.run", side_effect=AssertionError("Offline test")), \
                patch("src.agent.runtime.asyncio.create_subprocess_exec", side_effect=AssertionError("Offline test")), \
                patch("src.core.broadcast.broadcast.push", AsyncMock()):
            config = SimpleNamespace(agent={"enabled": True, "model_preset": "native"},
                                     llm_presets={"native": NATIVE}, get_active_llm_preset=lambda: NATIVE)
            runtime = AgentRuntime(SimpleNamespace(state=SimpleNamespace(config=config)), root=root, profile_loader=lambda _: {})
            try:
                self.assertEqual(preset_config_missing(NATIVE), [])
                preset = runtime.tool_model_preset()
                self.assertEqual(preset, NATIVE)
                run = runtime.prepare(SimpleNamespace(id="fixture", profile_id="fixture"), deepcopy(MESSAGES), preset)
                runtime._authorize(run)
                self.assertEqual(run.gateway.route["provider"], "google")
                self.assertTrue(run.gateway.route["native_google"])
                self.assertEqual(run.gateway.route["model"], MODEL)
                self.assertEqual(runtime.store.get_run(run.id)["state"], "running")
            finally:
                await runtime.close()


class WorkerProviderContractTests(unittest.TestCase):
    def test_worker_configures_native_and_compatible_sdks_without_host_secrets(self):
        """Run the real worker launcher with fake SDK/process/HTTP boundaries."""
        worker_path = Path(__file__).resolve().parents[1] / "integrations/deepseek-harness/worker.py"
        harness = Mock()
        with patch.dict(sys.modules, {"deepseek_harness": SimpleNamespace(DeepSeekHarness=harness)}):
            spec = importlib.util.spec_from_file_location("fixture_model_worker", worker_path)
            worker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(worker)
        for ptype in ("gemini", "openai_compat"):
            preset = {**NATIVE, "type": ptype, "base_url": "https://native.fixture"}
            route = model_route(preset)
            # This is exactly the non-secret route subset sent by AgentRuntime._drive.
            public_route = {key: route[key] for key in ("provider", "model", "native_google")}
            config = {"route": public_route, "timer_enabled": False, "envelope": {"messages": MESSAGES}}
            harness.reset_mock()
            harness.return_value.__enter__ = Mock()
            harness.return_value.__exit__ = Mock(return_value=False)
            with self.subTest(ptype=ptype), \
                    patch.object(worker.importlib.metadata, "version", return_value="0.2.1rc2"), \
                    patch.object(worker.sys, "stdin", io.StringIO(json.dumps(config) + "\n")), \
                    patch.object(worker.threading, "Thread"), \
                    patch.object(worker, "ThreadingHTTPServer"), \
                    patch.object(worker.Path, "write_text") as write_config, \
                    patch.object(worker, "_local", side_effect=[{}, [{"type": "completed"}]]), \
                    patch.object(worker, "emit"), patch.object(worker.time, "sleep"):
                worker.main()
            patches = json.loads(write_config.call_args.args[0])
            provider = patches[1]["config"]["providers"][route["provider"]]
            self.assertEqual(provider["baseURL"], "http://127.0.0.1:8090/" + (
                "v1beta" if ptype == "gemini" else "v1"))
            self.assertEqual(provider.get("api"), None if ptype == "gemini" else "openai-completions")
            self.assertEqual(provider["models"][0]["id"], route["model"])
            self.assertEqual(harness.call_args.kwargs["provider"], route["provider"])
            self.assertNotIn(NATIVE["api_key"], json.dumps(config) + json.dumps(patches) + str(harness.call_args))
