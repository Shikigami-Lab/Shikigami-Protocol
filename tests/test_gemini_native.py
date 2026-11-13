# -*- coding: utf-8 -*-
"""GeminiNativeProvider 离线测试：注入假 google-genai SDK，不打真实 API。"""
import asyncio
import json
import sys
import types as pytypes
import unittest
from unittest import mock

import httpx

from src.llm import registry
from src.llm.gemini_native import GeminiNativeProvider, _split_system


# ── 假 google-genai SDK ──────────────────────────────────────────────────────

class _FakeSafetySetting:
    def __init__(self, category=None, threshold=None):
        self.category = category
        self.threshold = threshold


class _FakeGenerateContentConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakePart:
    @staticmethod
    def from_text(text=""):
        return ("part", text)


class _FakeContent:
    def __init__(self, role=None, parts=None):
        self.role = role
        self.parts = parts


class _FakeHttpOptions:
    def __init__(self, base_url=None, timeout=None):
        self.base_url = base_url
        self.timeout = timeout


class _FakeTypes(pytypes.SimpleNamespace):
    pass


FAKE_TYPES = _FakeTypes(
    SafetySetting=_FakeSafetySetting,
    GenerateContentConfig=_FakeGenerateContentConfig,
    Part=_FakePart,
    Content=_FakeContent,
    HttpOptions=_FakeHttpOptions,
)


class _FakeChunk:
    def __init__(self, text):
        self.text = text


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _FakeModels:
    def __init__(self, client):
        self._client = client

    async def generate_content_stream(self, model=None, contents=None, config=None):
        self._client.calls.append(("stream", model, contents, config))
        return _FakeStream([_FakeChunk("你好"), _FakeChunk("，主人")])

    async def generate_content(self, model=None, contents=None, config=None):
        self._client.calls.append(("chat", model, contents, config))
        return _FakeChunk("完整回复")


class _FakeAio:
    def __init__(self, client):
        self.models = _FakeModels(client)


class _FakeClient:
    def __init__(self, api_key=None, http_options=None):
        self.api_key = api_key
        self.http_options = http_options
        self.calls = []

    @property
    def aio(self):
        return _FakeAio(self)


class _FakeGenai(pytypes.SimpleNamespace):
    Client = _FakeClient


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _patched_provider(**kwargs):
    """构造 provider 并把 _lazy_import 钉到假 SDK 上。"""
    with mock.patch(
        "src.llm.gemini_native._lazy_import",
        return_value=(_FakeGenai, FAKE_TYPES),
    ):
        provider = GeminiNativeProvider(**kwargs)
    provider._gen_kwargs.setdefault("temperature", 0.9)
    return provider


def _patch_import():
    return mock.patch(
        "src.llm.gemini_native._lazy_import",
        return_value=(_FakeGenai, FAKE_TYPES),
    )


# ── 消息拆分 ─────────────────────────────────────────────────────────────────

class TestSplitSystem(unittest.TestCase):
    def test_system_merged_and_roles_mapped(self):
        msgs = [
            {"role": "system", "content": "人格"},
            {"role": "system", "content": "补充"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "在的"},
            {"role": "user", "content": "嗯"},
        ]
        system, convo = _split_system(msgs)
        self.assertEqual(system, "人格\n\n补充")
        self.assertEqual(convo, [("user", "你好"), ("model", "在的"), ("user", "嗯")])

    def test_empty_content_skipped(self):
        system, convo = _split_system([{"role": "user", "content": ""}])
        self.assertEqual(system, "")
        self.assertEqual(convo, [])


# ── Provider 行为 ────────────────────────────────────────────────────────────

class TestGeminiNativeProvider(unittest.TestCase):
    def test_stream_yields_text_and_passes_config(self):
        provider = _patched_provider(
            api_key="k", model="gemini-2.5-flash",
            safety_settings=[
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_CIVIC_INTEGRITY"},  # 缺 threshold，应被过滤
            ],
        )
        with _patch_import():
            async def collect():
                return [t async for t in provider.stream_chat(
                    [{"role": "system", "content": "人格"}, {"role": "user", "content": "在吗"}]
                )]
            tokens = _run(collect())
        self.assertEqual("".join(tokens), "你好，主人")
        kind, model, contents, config = provider._client.calls[0]
        self.assertEqual(kind, "stream")
        self.assertEqual(model, "gemini-2.5-flash")
        self.assertEqual(config.kwargs["system_instruction"], "人格")
        ss = config.kwargs["safety_settings"]
        self.assertEqual(len(ss), 1)
        self.assertEqual(ss[0].category, "HARM_CATEGORY_HARASSMENT")
        self.assertEqual(ss[0].threshold, "BLOCK_NONE")
        # assistant → model 角色映射
        self.assertEqual([c.role for c in contents], ["user"])

    def test_no_safety_settings_omits_field(self):
        provider = _patched_provider(api_key="k", model="m")
        with _patch_import():
            _run(provider.chat([{"role": "user", "content": "hi"}]))
        _, _, _, config = provider._client.calls[0]
        self.assertNotIn("safety_settings", config.kwargs)

    def test_chat_returns_text(self):
        provider = _patched_provider(api_key="k", model="m")
        with _patch_import():
            out = _run(provider.chat([{"role": "user", "content": "hi"}]))
        self.assertEqual(out, "完整回复")

    def test_base_url_and_timeout_wired_into_client(self):
        provider = _patched_provider(
            api_key="k", model="m", base_url="https://proxy.example.com", timeout=12,
        )
        with _patch_import():
            provider._get_client()
        self.assertEqual(provider._client.http_options.base_url, "https://proxy.example.com")
        self.assertEqual(provider._client.http_options.timeout, 12000)

    def test_openai_compat_url_is_ignored(self):
        for url in (
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        ):
            provider = _patched_provider(api_key="k", model="m", base_url=url)
            self.assertIsNone(provider._base_url, url)
        # 原生协议的代理地址不受影响
        provider = _patched_provider(api_key="k", model="m", base_url="https://proxy.example.com")
        self.assertEqual(provider._base_url, "https://proxy.example.com")

    def test_max_tokens_maps_to_max_output_tokens(self):
        provider = _patched_provider(api_key="k", model="m", max_tokens=1024)
        with _patch_import():
            _run(provider.chat([{"role": "user", "content": "hi"}]))
        _, _, _, config = provider._client.calls[0]
        self.assertEqual(config.kwargs["max_output_tokens"], 1024)

    def test_call_token_limit_overrides_preset_in_chat_and_stream(self):
        for defaults in ({}, {"max_tokens": 8192}):
            for call in ({"max_tokens": 256}, {"max_output_tokens": 128}):
                with self.subTest(defaults=defaults, call=call), _patch_import():
                    provider = GeminiNativeProvider(api_key="k", model="m", **defaults)
                    _run(provider.chat([{"role": "user", "content": "hi"}], **call))

                    async def collect():
                        return [t async for t in provider.stream_chat(
                            [{"role": "user", "content": "hi"}], **call)]
                    _run(collect())
                    for _, _, _, config in provider._client.calls:
                        self.assertEqual(config.kwargs["max_output_tokens"], next(iter(call.values())))
                        self.assertNotIn("max_tokens", config.kwargs)

    def test_disabled_sampling_is_omitted_in_chat_and_stream(self):
        for disabled in (["temperature"], ["top_p"], ["temperature", "top_p"]):
            with self.subTest(disabled=disabled), _patch_import():
                provider = registry.get_provider({
                    "type": "gemini", "api_key": "k", "model": "m",
                    "temperature": .2, "top_p": .6, "unsupported_params": disabled,
                })
                _run(provider.chat([{"role": "user", "content": "hi"}], temperature=.3, top_p=.7))

                async def collect():
                    return [t async for t in provider.stream_chat(
                        [{"role": "user", "content": "hi"}], temperature=.3, top_p=.7)]
                _run(collect())
                for _, _, _, config in provider._client.calls:
                    for key, value in {"temperature": .3, "top_p": .7}.items():
                        if key in disabled:
                            self.assertNotIn(key, config.kwargs)
                        else:
                            self.assertEqual(config.kwargs[key], value)

    def test_custom_compatibility_gateway_requires_explicit_migration(self):
        for url in ("https://gateway.fixture/v1beta/openai/",
                    "https://gateway.fixture/openai/chat/completions"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "openai_compat"):
                registry.get_provider({"type": "gemini", "api_key": "k", "model": "m", "base_url": url})

    def test_missing_package_error_is_actionable(self):
        provider = GeminiNativeProvider(api_key="k", model="m")
        with mock.patch(
            "src.llm.gemini_native._lazy_import",
            side_effect=RuntimeError("gemini_native provider 需要 google-genai 包"),
        ):
            out = _run(provider.chat([{"role": "user", "content": "hi"}]))
        self.assertIn("google-genai", out)
        self.assertTrue(out.startswith("[LLM Error:"))


# ── 注册表接线 ───────────────────────────────────────────────────────────────

class TestRegistry(unittest.TestCase):
    def test_gemini_type_maps_to_native(self):
        provider = registry.get_provider({
            "type": "gemini", "api_key": "k", "model": "gemini-2.5-flash",
            "safety_settings": [{"category": "C", "threshold": "T"}],
        })
        self.assertIsInstance(provider, GeminiNativeProvider)
        self.assertEqual(provider._safety_settings, [{"category": "C", "threshold": "T"}])

    def test_openai_compat_unchanged(self):
        from src.llm.openai_compat import OpenAICompatProvider
        provider = registry.get_provider({"type": "openai_compat", "api_key": "k", "model": "m"})
        self.assertIsInstance(provider, OpenAICompatProvider)

    def test_compat_provider_tolerates_safety_settings_kwarg(self):
        # preset 里带 safety_settings 时 openai_compat 也不能炸（**kwargs 吞掉）
        from src.llm.openai_compat import OpenAICompatProvider
        provider = registry.get_provider({
            "type": "openai_compat", "api_key": "k", "model": "m",
            "safety_settings": [{"category": "C", "threshold": "T"}],
        })
        self.assertIsInstance(provider, OpenAICompatProvider)


class TestGeminiSDKWire(unittest.IsolatedAsyncioTestCase):
    async def test_installed_sdk_serializes_native_chat_and_stream_through_fake_http(self):
        try:
            from google import genai
        except ImportError:
            self.skipTest("Optional google-genai SDK is not installed")
        real_client = genai.Client
        for base in ("", "https://native.fixture/proxy", "https://generativelanguage.googleapis.com/v1beta/openai/"):
            sent = []

            def respond(request):
                sent.append(request)
                data = {"candidates": [{"content": {"role": "model", "parts": [{"text": "fixture"}]},
                                        "finishReason": "STOP"}]}
                if ":streamGenerateContent" in request.url.path:
                    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                          content="data: " + json.dumps(data) + "\n\n")
                return httpx.Response(200, json=data)

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                def make_client(**kwargs):
                    kwargs["http_options"].httpx_async_client = client
                    return real_client(vertexai=False, **kwargs)

                provider = GeminiNativeProvider(api_key="synthetic-key", base_url=base, model="gemini-2.5-flash",
                                                max_tokens=8192, temperature=.2, unsupported_params=["top_p"])
                try:
                    with self.subTest(base=base), mock.patch.object(genai, "Client", side_effect=make_client):
                        self.assertEqual(await provider.chat([{"role": "user", "content": "hi"}], max_tokens=256), "fixture")
                        self.assertEqual("".join([t async for t in provider.stream_chat(
                            [{"role": "user", "content": "hi"}], max_tokens=128)]), "fixture")
                    self.assertEqual(len(sent), 2)
                    expected_base = "https://native.fixture/proxy" if "native.fixture" in base else "https://generativelanguage.googleapis.com"
                    for request, method, limit in zip(sent, ("generateContent", "streamGenerateContent"), (256, 128)):
                        # SDK releases use v1 or v1beta by default; both are native.
                        self.assertIn(str(request.url).split("?")[0], [
                            expected_base + "/" + version + "/models/gemini-2.5-flash:" + method
                            for version in ("v1", "v1beta")])
                        self.assertEqual(request.headers["x-goog-api-key"], "synthetic-key")
                        self.assertEqual(json.loads(request.content)["generationConfig"],
                                         {"temperature": .2, "maxOutputTokens": limit})
                finally:
                    if provider._client is not None:
                        await provider._client.aio.aclose()
                        provider._client.close()


class TestPresetConfigMissing(unittest.TestCase):
    def test_gemini_needs_key_and_model_not_base_url(self):
        self.assertEqual(registry.preset_config_missing({
            "type": "gemini", "api_key": "k", "model": "m", "base_url": "",
        }), [])
        self.assertEqual(registry.preset_config_missing({
            "type": "gemini", "api_key": "", "model": "m",
        }), ["api_key"])
        self.assertEqual(registry.preset_config_missing({
            "type": "gemini", "api_key": "k", "model": "",
        }), ["model"])

    def test_compat_needs_base_url_and_model(self):
        self.assertEqual(registry.preset_config_missing({
            "type": "openai_compat", "base_url": "http://x", "model": "m",
        }), [])
        self.assertEqual(registry.preset_config_missing({
            "type": "openai_compat", "base_url": "", "model": "m", "api_key": "k",
        }), ["base_url"])
        # 未写 type 默认 openai_compat
        self.assertEqual(registry.preset_config_missing({"model": "m"}), ["base_url"])


if __name__ == "__main__":
    unittest.main()
