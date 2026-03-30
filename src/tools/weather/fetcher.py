"""WeatherFetcher — 从 wttr.in 抓取天气，缓存到 profiles/{id}/weather_cache.json。

免费无需 API Key。格式：wttr.in/{city}?format=j1
失败静默，不影响主流程。
"""
import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_WTTR_URL = "https://wttr.in/{city}?format=j1"
_TIMEOUT = 8.0
_DEFAULT_CITY = "auto"    # auto = IP 定位


async def fetch_weather(city: str = _DEFAULT_CITY) -> Optional[Dict[str, Any]]:
    """抓取天气，返回标准化字典或 None。"""
    url = _WTTR_URL.format(city=city if city and city != "auto" else "")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        current = data.get("current_condition", [{}])[0]
        temp_c = current.get("temp_C", "")
        feels_c = current.get("FeelsLikeC", "")
        desc_list = current.get("weatherDesc", [{}])
        desc = (desc_list[0].get("value", "") if desc_list else "").strip()
        humidity = current.get("humidity", "")
        weather_code = current.get("weatherCode", "")

        # 获取城市名（wttr.in 不直接返回，用请求的 city）
        nearest = data.get("nearest_area", [{}])[0]
        city_name = ""
        area_name = nearest.get("areaName", [{}])
        if area_name:
            city_name = area_name[0].get("value", city)

        return {
            "city": city_name or city,
            "temp_c": temp_c,
            "feels_like_c": feels_c,
            "description": desc,
            "humidity": humidity,
            "weather_code": weather_code,
            "fetched_at": time.time(),
        }
    except Exception as e:
        logger.warning("[WeatherFetcher] fetch error city=%s: %s", city, e)
        return None


def load_cache(storage_root: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(storage_root, "weather_cache.json")
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def save_cache(storage_root: str, data: Dict[str, Any]):
    path = os.path.join(storage_root, "weather_cache.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


async def run_fetch_once(profile_id: str, storage_root: str, city: str = _DEFAULT_CITY) -> bool:
    """抓取天气并写入缓存。成功返回 True。"""
    from src.utils.debug_logger import log_tool_weather
    result = await fetch_weather(city)
    if result:
        save_cache(storage_root, result)
        log_tool_weather(action="fetch", profile_id=profile_id, city=result.get("city", city),
                         temp_c=result.get("temp_c", ""), description=result.get("description", ""))
        logger.info("[WeatherFetcher] profile=%s city=%s temp=%s°C desc=%s",
                    profile_id, result.get("city"), result.get("temp_c"), result.get("description"))
        return True
    return False


async def weather_fetcher_loop(app):
    """主天气抓取循环，在 server.py 启动时作为 asyncio task 运行。"""
    _last_fetch: Dict[str, float] = {}
    _CHECK_INTERVAL = 300   # 5min 检查一次是否需要抓取

    logger.info("[WeatherFetcher] loop started")

    while True:
        try:
            sm = getattr(getattr(app, "state", None), "session_manager", None)
            if sm:
                for session in sm.list_sessions():
                    try:
                        import json as _json
                        profile_path = os.path.join("profiles", f"{session.profile_id}.json")
                        if not os.path.exists(profile_path):
                            continue
                        with open(profile_path, encoding="utf-8") as f:
                            profile_data = _json.load(f)

                        # weather 配置从 tool_configs 读取
                        tool_cfgs = profile_data.get("tool_configs") or {}
                        weather_cfg = tool_cfgs.get("weather") or {}
                        if not weather_cfg.get("enabled", False):
                            continue

                        interval = int(weather_cfg.get("fetch_interval_seconds", 3600))
                        city = weather_cfg.get("location", _DEFAULT_CITY) or _DEFAULT_CITY
                        last = _last_fetch.get(session.profile_id, 0.0)
                        if time.time() - last < interval:
                            continue

                        _last_fetch[session.profile_id] = time.time()
                        await run_fetch_once(session.profile_id, session.storage_root, city)

                    except Exception as e:
                        logger.warning("[WeatherFetcher] session=%s error: %s", session.id, e)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[WeatherFetcher] loop error: %s", e)

        await asyncio.sleep(_CHECK_INTERVAL)
