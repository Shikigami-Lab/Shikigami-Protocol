"""Tools API — 工具元数据与 per-profile 配置管理。

GET    /api/tools                                  — 返回注册表中所有工具 + 当前 profile 的 tool_configs
PATCH  /api/profiles/{profile_id}/tool_configs    — 保存某工具的配置到 profile JSON
POST   /api/profiles/{profile_id}/trend/fetch     — 手动触发一次趋势抓取（调试用）
GET    /api/profiles/{profile_id}/trend/status    — 返回趋势缓存状态（条目数、最后抓取时间）
DELETE /api/profiles/{profile_id}/trend/cache     — 清空趋势缓存
GET    /api/profiles/{profile_id}/weather/status  — 返回天气缓存状态
"""
import json
import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

_PROFILES_DIR = "profiles"


def _load_profile(profile_id: str) -> Dict[str, Any]:
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Profile not found")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_profile(profile_id: str, data: Dict[str, Any]):
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ── GET /api/tools ─────────────────────────────────────────────────────────────

@router.get("/api/tools")
async def list_tools(request: Request, profile_id: str = ""):
    """返回所有注册工具元数据，附带指定 profile 的 tool_configs 覆盖值。
    profile_id 为空时使用当前活跃 session 的 profile。
    """
    from src.tools.registry import get_all_tools
    profile_data: Dict[str, Any] = {}
    if not profile_id:
        sm = request.app.state.session_manager
        current = sm.get_current()
        if current:
            profile_id = current.profile_id
    if profile_id:
        try:
            profile_data = _load_profile(profile_id)
        except Exception:
            pass

    tools = get_all_tools()
    result = []
    for t in tools:
        tool_id = t["tool_id"]
        default_cfg = t.get("default_config", {})

        # Merge: default_config <- profile tool_configs override
        # trend uses trend_config key; weather/others use tool_configs.{tool_id}
        if tool_id == "trend":
            profile_cfg = profile_data.get("trend_config") or {}
        else:
            tool_cfgs = profile_data.get("tool_configs") or {}
            profile_cfg = tool_cfgs.get(tool_id) or {}

        merged = {**default_cfg, **profile_cfg}
        result.append({**t, "config": merged})

    return {"tools": result}


# ── PATCH /api/profiles/{profile_id}/tool_configs ──────────────────────────────

class ToolConfigPatch(BaseModel):
    tool_id: str
    config: Dict[str, Any]


@router.patch("/api/profiles/{profile_id}/tool_configs")
async def patch_tool_config(profile_id: str, body: ToolConfigPatch):
    """保存某工具的配置到 profile JSON。trend 工具写入 trend_config，其余写入 tool_configs.{tool_id}。"""
    try:
        data = _load_profile(profile_id)
    except HTTPException:
        raise

    tool_id = body.tool_id
    cfg = body.config

    if tool_id == "trend":
        existing = data.get("trend_config") or {}
        data["trend_config"] = {**existing, **cfg}
    else:
        tool_cfgs = data.get("tool_configs") or {}
        existing = tool_cfgs.get(tool_id) or {}
        tool_cfgs[tool_id] = {**existing, **cfg}
        data["tool_configs"] = tool_cfgs

    _save_profile(profile_id, data)
    return {"ok": True}


# ── POST /api/profiles/{profile_id}/trend/fetch ────────────────────────────────

@router.post("/api/profiles/{profile_id}/trend/fetch")
async def manual_trend_fetch(profile_id: str):
    """手动触发一次趋势抓取（调试 / 「立即抓取」按钮）。"""
    try:
        data = _load_profile(profile_id)
    except HTTPException:
        raise

    storage_root = os.path.join(_PROFILES_DIR, profile_id)
    try:
        from src.tools.trends.fetcher import run_fetch_once
        added = await run_fetch_once(profile_id, storage_root, data)
        return {"ok": True, "added": added}
    except Exception as e:
        logger.warning("[tools_api] manual trend fetch error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/profiles/{profile_id}/trend/status ────────────────────────────────

@router.get("/api/profiles/{profile_id}/trend/status")
async def trend_status(profile_id: str):
    storage_root = os.path.join(_PROFILES_DIR, profile_id)
    try:
        from src.tools.trends.store import TrendStore
        store = TrendStore(storage_root)
        return {
            "unused_count": store.count_unused(),
            "total_count": len(store._items),
            "last_fetched_at": store.last_fetched_at(),
        }
    except Exception as e:
        return {"unused_count": 0, "total_count": 0, "last_fetched_at": None, "error": str(e)}


# ── DELETE /api/profiles/{profile_id}/trend/cache ──────────────────────────────

@router.delete("/api/profiles/{profile_id}/trend/cache")
async def trend_clear_cache(profile_id: str):
    storage_root = os.path.join(_PROFILES_DIR, profile_id)
    try:
        from src.tools.trends.store import TrendStore
        store = TrendStore(storage_root)
        store.clear_all()
        return {"ok": True}
    except Exception as e:
        logger.warning("[tools_api] trend clear cache error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/profiles/{profile_id}/weather/status ──────────────────────────────

@router.get("/api/profiles/{profile_id}/weather/status")
async def weather_status(profile_id: str):
    storage_root = os.path.join(_PROFILES_DIR, profile_id)
    try:
        from src.tools.weather.fetcher import load_cache
        import time
        cache = load_cache(storage_root)
        if not cache:
            return {"available": False}
        age_seconds = int(time.time() - cache.get("fetched_at", 0))
        return {
            "available": True,
            "city": cache.get("city", ""),
            "temp_c": cache.get("temp_c", ""),
            "description": cache.get("description", ""),
            "age_seconds": age_seconds,
        }
    except Exception as e:
        return {"available": False, "error": str(e)}
