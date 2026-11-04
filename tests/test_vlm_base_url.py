import unittest
from types import SimpleNamespace
from unittest import mock

from src.api.vlm import _call_vlm
from src.config.app_config import AppConfig
from src.core.ase import AseEngine


class VlmBaseUrlTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_base_url(self, configured_url, expected_url):
        preset = {"base_url": configured_url, "model": "fixture-vision"}
        original_preset = dict(preset)
        create = mock.AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Fixture description."))]
        ))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch("openai.AsyncOpenAI", return_value=client) as client_factory, \
                mock.patch("src.api.vlm.get_prompt", return_value="Describe the fixture."), \
                mock.patch("src.api.vlm.log_secondary_llm_call"), \
                mock.patch("src.api.vlm.log_secondary_llm_response"):
            description = await _call_vlm(preset, "iVBORw0KGgo=", image_type="upload")

        self.assertEqual("Fixture description.", description)
        self.assertEqual(expected_url, client_factory.call_args.kwargs["base_url"])
        self.assertEqual(original_preset, preset)
        create.assert_awaited_once()
        self.assertEqual("fixture-vision", create.call_args.kwargs["model"])

    async def test_full_completion_endpoint_is_normalized(self):
        await self._assert_base_url(
            "https://example.invalid/v1/chat/completions", "https://example.invalid/v1"
        )

    async def test_full_endpoint_with_trailing_slash_and_whitespace_is_normalized(self):
        await self._assert_base_url(
            "  https://example.invalid/v1/chat/completions/  ", "https://example.invalid/v1"
        )

    async def test_api_root_is_preserved(self):
        for base_url in ("https://example.invalid/v1", "https://example.invalid/v1/"):
            with self.subTest(base_url=base_url):
                await self._assert_base_url(base_url, base_url)

    async def test_empty_base_url_keeps_sdk_default(self):
        for base_url in (None, ""):
            with self.subTest(base_url=base_url):
                await self._assert_base_url(base_url, None)


class ProactiveScreenshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_vision_model_does_not_request_or_send_screenshot(self):
        config = AppConfig(
            default_llm="chat", llm_presets={"chat": {"api_key": "synthetic", "model": "chat"}},
        )
        engine = AseEngine(SimpleNamespace(state=SimpleNamespace(config=config)))
        session = SimpleNamespace(id="synthetic", last_screenshot_b64=None)
        for name in ("__none__", "deleted"):
            with self.subTest(name=name):
                config.vlm = {"enabled": True, "model_preset": name, "ase_wait_seconds": 0}
                with mock.patch("src.core.broadcast.broadcast.push", new_callable=mock.AsyncMock) as push, \
                        mock.patch("src.api.vlm._call_vlm", new_callable=mock.AsyncMock) as describe:
                    self.assertEqual("", await engine._request_screenshot_and_describe(session))
                push.assert_not_awaited()
                describe.assert_not_awaited()


    async def test_explicit_active_vision_model_can_describe_screenshot(self):
        preset = {"api_key": "synthetic", "model": "fixture-vision"}
        config = AppConfig(default_llm="chat", llm_presets={"chat": preset})
        config.vlm = {"enabled": True, "model_preset": "__active__", "ase_wait_seconds": 0}
        engine = AseEngine(SimpleNamespace(state=SimpleNamespace(config=config)))
        session = SimpleNamespace(id="synthetic", last_screenshot_b64=None)

        async def provide_fixture(_session_id, _event):
            session.last_screenshot_b64 = "fixture-screenshot"

        with mock.patch("src.core.broadcast.broadcast.push", side_effect=provide_fixture), \
                mock.patch("src.api.vlm._call_vlm", new_callable=mock.AsyncMock, return_value="fixture") as describe:
            self.assertEqual("fixture", await engine._request_screenshot_and_describe(session))
        describe.assert_awaited_once_with(preset, "fixture-screenshot", session_id="synthetic")


if __name__ == "__main__":
    unittest.main()
