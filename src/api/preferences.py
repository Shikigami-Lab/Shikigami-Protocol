"""
UI preferences — lightweight key/value store persisted to config/ui_prefs.json.
Used for settings that must survive across browser sessions / different clients
(e.g. display language).  Not profile-specific.
"""
import json
import os
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

_PREFS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                           "config", "ui_prefs.json")

_ALLOWED_KEYS = {"locale"}   # whitelist — add keys here as needed


def _read() -> dict:
    try:
        with open(_PREFS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write(data: dict) -> None:
    os.makedirs(os.path.dirname(_PREFS_FILE), exist_ok=True)
    tmp = _PREFS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _PREFS_FILE)


@router.get("/api/preferences")
async def get_preferences():
    return JSONResponse(_read())


@router.post("/api/preferences")
async def set_preferences(request: Request):
    body = await request.json()
    prefs = _read()
    for k, v in body.items():
        if k in _ALLOWED_KEYS:
            prefs[k] = v
    _write(prefs)
    return JSONResponse(prefs)
