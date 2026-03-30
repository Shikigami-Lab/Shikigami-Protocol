import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/settings")
async def get_settings(request: Request):
    config = request.app.state.config
    return {
        "default_llm": config.default_llm,
        "default_tts": config.default_tts,
        # Only expose non-secret fields
        "llm_presets": {
            name: {
                "base_url": p.get("base_url", ""),
                "model": p.get("model", ""),
                "has_key": bool(p.get("api_key", "")),
            }
            for name, p in config.llm_presets.items()
        },
    }


class UpdateLLMRequest(BaseModel):
    preset_name: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None


@router.post("/settings/llm")
async def update_llm_preset(request: Request, body: UpdateLLMRequest):
    """Update an LLM preset at runtime (does not persist to app.yaml)."""
    config = request.app.state.config
    preset = config.llm_presets.setdefault(body.preset_name, {})
    if body.api_key is not None:
        preset["api_key"] = body.api_key
    if body.base_url is not None:
        preset["base_url"] = body.base_url
    if body.model is not None:
        preset["model"] = body.model
    config.default_llm = body.preset_name
    return {"ok": True, "preset": body.preset_name}
