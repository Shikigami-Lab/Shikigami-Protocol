"""WebSearch tool — call register(app) once to wire commands."""
import logging

logger = logging.getLogger(__name__)

MANIFEST = {
    "tool_id": "websearch",
    "label": "网络搜索",
    "type": "interactive",
    "agent_capable": True,
    "description": "搜索网络信息，结果经 AI 过滤后以角色风格回复。也作为趋势感知的数据源。",
    "has_segment": False,
    "has_commands": True,
    "has_fetcher": False,
    "default_config": {
        "enabled": True,
        "serper_api_key": "",  # 留空使用免费 DuckDuckGo；填入则切换到 Serper
    },
}


def register(app) -> None:
    from src.tools.registry import register_tool
    register_tool(MANIFEST)  # has_commands 驱动约定模块接线（注册 /search）

    logger.info("[tools.websearch] registered")
