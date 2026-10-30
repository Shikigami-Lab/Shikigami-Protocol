import logging

from fastapi import APIRouter, Request

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


# 旧端点 POST /settings/llm 已删除：它运行时改 preset 且不持久化、不刷新 analysis provider，
# 与 settings_ext.py 的 preset CRUD（持久化到 .env / app.yaml）语义冲突。统一用后者。
