import unittest
from types import SimpleNamespace
from unittest import mock

from src.api.vlm import _call_vlm


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


if __name__ == "__main__":
    unittest.main()
