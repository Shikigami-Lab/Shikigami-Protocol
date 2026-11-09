import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.agent.business_tools import WeatherQueryArgs, _weather_query, call_business_tool
from src.agent.capabilities import business_tools, capabilities
from src.agent.settings import normalize_settings

FETCHER = "src.tools.weather.fetcher"
WEATHER = {"city": "Shanghai", "temp_c": "18", "feels_like_c": "16",
           "description": "Light rain", "humidity": "80", "weather_code": "296",
           "fetched_at": 1762700000.0}


class WeatherQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.session = SimpleNamespace(storage_root=self._tmp.name)

    async def test_named_city_fetches_live_weather(self):
        with patch(f"{FETCHER}.fetch_weather", new=AsyncMock(return_value=dict(WEATHER))) as fetch, \
             patch(f"{FETCHER}.load_cache") as cache:
            result = await _weather_query(self.session, WeatherQueryArgs(city="Shanghai"))
        self.assertEqual(result, {"ok": True, "source": "wttr.in", "cached": False, "weather": WEATHER})
        fetch.assert_awaited_once_with("Shanghai")
        cache.assert_not_called()

    async def test_named_city_failure_is_an_error_not_empty_success(self):
        with patch(f"{FETCHER}.fetch_weather", new=AsyncMock(return_value=None)):
            result = await _weather_query(self.session, WeatherQueryArgs(city="Nowhere"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "weather_unavailable")

    async def test_blank_city_reads_cache_without_fetching(self):
        with patch(f"{FETCHER}.load_cache", return_value=dict(WEATHER)), \
             patch(f"{FETCHER}.fetch_weather", new=AsyncMock()) as fetch:
            result = await _weather_query(self.session, WeatherQueryArgs())
        self.assertEqual(result, {"ok": True, "source": "weather_cache", "cached": True, "weather": WEATHER})
        fetch.assert_not_awaited()

    async def test_blank_city_without_cache_falls_back_to_one_live_fetch(self):
        with patch(f"{FETCHER}.load_cache", return_value=None), \
             patch(f"{FETCHER}.fetch_weather", new=AsyncMock(return_value=dict(WEATHER))) as fetch:
            result = await _weather_query(self.session, WeatherQueryArgs(city="  "))
        self.assertEqual(result, {"ok": True, "source": "wttr.in", "cached": False, "weather": WEATHER})
        fetch.assert_awaited_once_with()

    async def test_blank_city_without_cache_or_fetch_is_an_error(self):
        with patch(f"{FETCHER}.load_cache", return_value=None), \
             patch(f"{FETCHER}.fetch_weather", new=AsyncMock(return_value=None)):
            result = await _weather_query(self.session, WeatherQueryArgs())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "weather_unavailable")

    async def test_dispatch_validates_session_and_arguments(self):
        bound = SimpleNamespace(storage_root=self._tmp.name, profile_id="p1", id="p1")
        with patch(f"{FETCHER}.load_cache", return_value=dict(WEATHER)):
            result = await call_business_tool(None, bound, "weather_query", {})
        self.assertTrue(result["ok"])
        result = await call_business_tool(None, SimpleNamespace(profile_id="p1", id="other"), "weather_query", {})
        self.assertEqual(result["error"]["code"], "invalid_session")
        result = await call_business_tool(None, bound, "weather_query", {"city": 5})
        self.assertEqual(result["error"]["code"], "invalid_arguments")


class WeatherCapabilityTests(unittest.TestCase):
    def test_weather_tool_follows_the_profile_weather_switch(self):
        settings = normalize_settings({"enabled": True})
        self.assertNotIn("weather_query", business_tools(capabilities(settings, {})))
        profile = {"tool_configs": {"weather": {"enabled": True}}}
        self.assertIn("weather_query", business_tools(capabilities(settings, profile)))
        profile = {"tool_configs": {"weather": {"enabled": False}}}
        self.assertNotIn("weather_query", business_tools(capabilities(settings, profile)))


if __name__ == "__main__":
    unittest.main()
