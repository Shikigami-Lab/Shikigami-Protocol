"""Weather Tool — 感知型工具，后台抓取天气注入 AI 情境感知。"""
from src.tools.registry import register_tool

MANIFEST = {
    "tool_id": "weather",
    "label": "天气感知",
    "type": "awareness",
    "agent_capable": True,
    "description": "后台抓取实时天气（wttr.in，无需 key），注入 prompt 增强情境感知",
    "has_segment": True,
    "has_commands": False,
    "has_fetcher": True,
    # 段在包外（src/prompt/segments/weather.py），显式声明接线目标；
    # 注入由段的 requires=("tool:weather",) 门控。
    "segment_module": "src.prompt.segments.weather",
    "default_config": {
        "enabled": False,
        "location": "",
        "fetch_interval_seconds": 3600,
    },
}


def register(app):
    register_tool(MANIFEST)
