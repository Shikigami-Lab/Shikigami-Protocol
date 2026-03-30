"""Weather Tool — 感知型工具，后台抓取天气注入 AI 情境感知。"""
from src.tools.registry import register_tool

MANIFEST = {
    "tool_id": "weather",
    "label": "天气感知",
    "type": "awareness",
    "description": "后台抓取实时天气（wttr.in，无需 key），注入 prompt 增强情境感知",
    "has_segment": True,
    "has_commands": False,
    "has_fetcher": True,
    "default_config": {
        "enabled": False,
        "location": "",
        "fetch_interval_seconds": 3600,
    },
}


def register(app):
    register_tool(MANIFEST)
    import src.prompt.segments.weather  # noqa: F401  trigger @register
