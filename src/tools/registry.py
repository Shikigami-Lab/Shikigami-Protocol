"""Tool Registry — 统一的工具发现与注册机制。

每个工具在 __init__.py 的 register(app) 入口中调用 register_tool(MANIFEST)。
设置 UI 通过 GET /api/tools 读取所有工具元数据 + 当前 profile 的 tool_configs。
"""
from typing import Dict, List, Optional

_registry: Dict[str, dict] = {}


def register_tool(manifest: dict) -> None:
    """注册一个工具。manifest 必须包含 tool_id 字段。"""
    tool_id = manifest.get("tool_id")
    if not tool_id:
        raise ValueError("manifest must have tool_id")
    _registry[tool_id] = manifest


def get_all_tools() -> List[dict]:
    return list(_registry.values())


def get_tool(tool_id: str) -> Optional[dict]:
    return _registry.get(tool_id)
