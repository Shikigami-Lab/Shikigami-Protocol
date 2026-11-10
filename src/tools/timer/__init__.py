"""Timer tool — call register(app) once to wire up commands and active_timers segment."""
import logging

logger = logging.getLogger(__name__)

MANIFEST = {
    "tool_id": "timer",
    "label": "计时器",
    "type": "interactive",
    "agent_capable": True,
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
    register_tool(MANIFEST)  # has_commands/has_segment 驱动约定模块接线

    logger.info("[tools.timer] registered")
