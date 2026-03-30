"""Memory 模块：长期事实、向量、摘要等。"""
import logging
from typing import Any

logger = logging.getLogger(__name__)


def log_startup_config(config: Any) -> None:
    """在进程启动时打记忆模块配置日志（由 server lifespan 调用）。"""
    mem = config.get_memory_config() if hasattr(config, "get_memory_config") else {}
    logger.info(
        "[memory] 配置: enabled=%s vector_enabled=%s extraction_freq=%s day_summary=%s",
        mem.get("enabled"),
        mem.get("vector_enabled"),
        mem.get("extraction_frequency"),
        mem.get("day_summary_enabled"),
    )
