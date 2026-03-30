"""Timer tool — call register(app) once to wire up commands and active_timers segment."""
import logging

logger = logging.getLogger(__name__)

MANIFEST = {
    "tool_id": "timer",
    "label": "计时器",
    "type": "interactive",
    "description": "倒计时计时器，到期后 AI 会主动提醒（后端直接触发，不依赖客户端在线）",
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

    import src.tools.timer.commands  # noqa: F401  — registers command on import
    import src.tools.timer.segment   # noqa: F401  — registers segment on import

    logger.info("[tools.timer] registered")
