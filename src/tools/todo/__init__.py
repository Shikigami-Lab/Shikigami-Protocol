"""Todo tool — call register(app) once to wire commands and prompt segment."""
import logging

logger = logging.getLogger(__name__)

MANIFEST = {
    "tool_id": "todo",
    "label": "待办事项",
    "type": "interactive",
    "agent_capable": True,
    "description": "管理待办清单，AI 会在合适时机提醒未完成的事项",
    "has_segment": True,
    "has_commands": True,
    "has_fetcher": False,
    "default_config": {
        "enabled": True,
    },
}


def register(app) -> None:
    from src.tools.registry import register_tool
    register_tool(MANIFEST)  # has_commands/has_segment 驱动约定模块接线

    logger.info("[tools.todo] registered")
