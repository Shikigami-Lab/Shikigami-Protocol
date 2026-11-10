"""Tool Registry — 统一的工具发现与注册机制。

每个工具在 __init__.py 的 register(app) 入口中调用 register_tool(MANIFEST)。
MANIFEST 不只描述 UI 元数据，还驱动接线：has_commands / has_segment 为真时，
register_tool 会导入对应模块完成命令/段注册——默认按约定路径
``src.tools.<tool_id>.commands`` / ``.segment``，也可用 ``commands_module`` /
``segment_module`` 显式指定（如 weather 的段在 src/prompt/segments 下）。

设置 UI 通过 GET /api/tools 读取所有工具元数据 + 当前 profile 的 tool_configs。
"""
import importlib
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_registry: Dict[str, dict] = {}


def register_tool(manifest: dict) -> None:
    """注册一个工具。manifest 必须包含 tool_id 字段。

    has_commands/has_segment 为真时同步完成接线（导入模块触发其注册钩子）。
    """
    tool_id = manifest.get("tool_id")
    if not tool_id:
        raise ValueError("manifest must have tool_id")
    _registry[tool_id] = manifest
    _wire_tool_modules(tool_id, manifest)


def _wire_tool_modules(tool_id: str, manifest: dict) -> None:
    for flag, suffix, key in (("has_commands", "commands", "commands_module"),
                              ("has_segment", "segment", "segment_module")):
        if not manifest.get(flag):
            continue
        module = manifest.get(key) or f"src.tools.{tool_id}.{suffix}"
        try:
            importlib.import_module(module)
        except ImportError:
            if key in manifest:
                raise  # 显式声明的模块必须存在
            logger.warning("[tools.%s] %s=True 但约定模块 %s 不存在", tool_id, flag, module)


def get_all_tools() -> List[dict]:
    return list(_registry.values())


def get_tool(tool_id: str) -> Optional[dict]:
    return _registry.get(tool_id)
