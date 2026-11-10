"""Tools API — 工具元数据与 per-profile 配置管理。

GET    /api/tools                                  — 返回注册表中所有工具 + 当前 profile 的 tool_configs
PATCH  /api/profiles/{profile_id}/tool_configs    — 保存某工具的配置到 profile JSON
GET    /api/profiles/{profile_id}/weather/status  — 返回天气缓存状态
"""
import json
import os
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.utils.paths import get_project_root

router = APIRouter()

_PROFILES_DIR = os.path.join(get_project_root(), "profiles")


def _load_profile(profile_id: str) -> Dict[str, Any]:
    if (not profile_id or profile_id in ('.', '..') or len(profile_id) > 256
            or any(c in profile_id for c in '/\\:') or any(ord(c) < 32 for c in profile_id)):
        raise HTTPException(status_code=400, detail='Invalid character identity')
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
    tool_cfgs = profile_data.get("tool_configs") or {}
    result = []
    for t in tools:
        tool_id = t["tool_id"]
        default_cfg = t.get("default_config", {})

        profile_cfg = tool_cfgs.get(tool_id) or {}
        merged = {**default_cfg, **profile_cfg}
        if t.get('agent_capable'):
            merged['enabled'] = merged.get('enabled', True) is True
        result.append({**t, "config": merged})

    return {"tools": result}


# ── PATCH /api/profiles/{profile_id}/tool_configs ──────────────────────────────

class ToolConfigPatch(BaseModel):
    tool_id: str
    config: Dict[str, Any]


@router.patch("/api/profiles/{profile_id}/tool_configs")
async def patch_tool_config(profile_id: str, body: ToolConfigPatch, request: Request):
    """保存某工具的配置到 profile JSON 的 tool_configs.{tool_id}。"""
    from src.api.agent import same_origin
    same_origin(request)
    try:
        data = _load_profile(profile_id)
    except HTTPException:
        raise

    tool_id = body.tool_id
    cfg = body.config
    if tool_id == 'agent':
        from src.agent.capabilities import KEYS
        if any(key not in (*KEYS, 'enabled', 'autonomy_enabled') or (value is not None and type(value) is not bool) for key, value in cfg.items()):
            raise HTTPException(status_code=422, detail='Agent settings must be known boolean or null (inherit) fields')
    if tool_id in ('timer', 'todo') and 'enabled' in cfg and type(cfg['enabled']) is not bool:
        raise HTTPException(status_code=422, detail='Tool permission must be a boolean')

    tool_cfgs = data.get("tool_configs") or {}
    existing = tool_cfgs.get(tool_id) or {}
    tool_cfgs[tool_id] = {**existing, **cfg}
    data["tool_configs"] = tool_cfgs

    _save_profile(profile_id, data)
    if tool_id == 'agent' or (tool_id == 'todo' and cfg.get('enabled') is False):
        runtime = getattr(request.app.state, 'agent_runtime', None)
        if runtime:
            for run in list(runtime._runs.values()):
                if run.profile_id == profile_id and not run.terminal and not runtime.permissions_valid(run):
                    await runtime.cancel(run.id, profile_id)
    return {"ok": True}


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
