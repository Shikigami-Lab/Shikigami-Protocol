"""WeatherSegment — priority=79，将天气信息注入 prompt。

cooldown 模式（默认 2h），读取 weather_cache.json（由 WeatherFetcher 后台更新）。
缓存超过 6h 视为陈旧，不注入（避免提供过时信息）。
"""
import logging
import os
import time
from typing import Optional, Dict, Any

from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)

_STALE_SECONDS = 6 * 3600    # 缓存超过 6h 不注入

_WEATHER_CODE_MAP = {
    "113": "☀️", "116": "⛅", "119": "🌥️", "122": "☁️",
    "143": "🌫️", "176": "🌦️", "185": "🌨️", "200": "⛈️",
    "227": "🌨️", "230": "❄️", "248": "🌫️", "260": "🌫️",
    "263": "🌦️", "266": "🌧️", "281": "🌨️", "284": "🌨️",
    "293": "🌦️", "296": "🌧️", "299": "🌧️", "302": "🌧️",
    "305": "🌧️", "308": "🌧️", "311": "🌨️", "314": "🌨️",
    "317": "🌨️", "320": "🌨️", "323": "🌨️", "326": "❄️",
    "329": "❄️", "332": "❄️", "335": "❄️", "338": "❄️",
    "350": "🌨️", "353": "🌦️", "356": "🌧️", "359": "🌧️",
    "362": "🌨️", "365": "🌨️", "368": "🌨️", "371": "❄️",
    "374": "🌨️", "377": "🌨️", "386": "⛈️", "389": "⛈️",
    "392": "🌨️", "395": "❄️",
}


@register
class WeatherSegment(PromptSegment):
    segment_id = "weather"
    priority = 79
    label = "天气感知"
    description = "将当前天气信息注入 prompt，增强 AI 的时空情境感"
    is_core = False
    inject_into = "chat"
    is_readonly = True
    default_trigger_mode = "cooldown"
    default_trigger_param = 120.0   # 分钟，默认 2h

    def build(self, ctx: BuildContext) -> SegmentResult:
        tool_cfgs = (ctx.profile or {}).get("tool_configs") or {}
        weather_cfg = tool_cfgs.get("weather") or {}
        if not weather_cfg.get("enabled", False):
            return SegmentResult(fired=False)

        storage_root = getattr(ctx.session, "storage_root", "")
        if not storage_root:
            return SegmentResult(fired=False)

        try:
            from src.tools.weather.fetcher import load_cache
            cache = load_cache(storage_root)
        except Exception as e:
            logger.debug("[WeatherSegment] load_cache error: %s", e)
            return SegmentResult(fired=False)

        if not cache:
            return SegmentResult(fired=False)

        fetched_at = cache.get("fetched_at", 0)
        if time.time() - fetched_at > _STALE_SECONDS:
            logger.debug("[WeatherSegment] weather cache stale, skipping")
            return SegmentResult(fired=False)

        city = cache.get("city", "")
        temp = cache.get("temp_c", "")
        feels = cache.get("feels_like_c", "")
        desc = cache.get("description", "")
        humidity = cache.get("humidity", "")
        code = cache.get("weather_code", "")

        emoji = _WEATHER_CODE_MAP.get(str(code), "")
        parts = []
        if city:
            parts.append(city)
        if temp:
            parts.append(f"{temp}°C")
        if feels and feels != temp:
            parts.append(f"体感 {feels}°C")
        if desc:
            parts.append(f"{emoji} {desc}" if emoji else desc)
        if humidity:
            parts.append(f"湿度 {humidity}%")

        if not parts:
            return SegmentResult(fired=False)

        text = f"[当前天气] {' · '.join(parts)}"
        logger.debug("[WeatherSegment] injected: %s", text)
        return SegmentResult(
            messages=[{"role": "system", "content": text}],
        )
