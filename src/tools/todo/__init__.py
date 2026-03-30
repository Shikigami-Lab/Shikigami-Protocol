"""Todo tool — call register(app) once to wire commands and prompt segment."""
import logging

logger = logging.getLogger(__name__)

MANIFEST = {
    "tool_id": "todo",
    "label": "待办事项",
    "type": "interactive",
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
    register_tool(MANIFEST)

    import src.tools.todo.commands  # noqa: F401  — registers command on import
    import src.tools.todo.segment   # noqa: F401  — registers segment on import

    logger.info("[tools.todo] registered")
