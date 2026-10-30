"""Trend Awareness Tool — 感知型工具，后台抓取网络趋势注入 AI 内心世界。

趋势走 Reflection → ASE 路径：
  TrendFetcher 抓取 → trend_cache.json
  → TrendContextReflectionSegment 在反省时取 get_unused(3) 并标记已读
  → Reflection LLM 生成 topic_anchor
  → ASE 在用户沉默时主动发言引用该话题

趋势不注入聊天 pipeline，不会在对话中途突然出现。
"""
from src.tools.registry import register_tool

MANIFEST = {
    "tool_id": "trend",
    "label": "趋势感知",
    "type": "awareness",
    "description": "后台定期抓取 RSS/搜索数据，AI 沉默时通过主动发言自然提及——不在对话中插入",
    "has_segment": False,
    "has_commands": False,
    "has_fetcher": True,
    "default_config": {
        "enabled": False,
        "fetch_interval_seconds": 7200,
        "max_cached_items": 20,
        "sources": [],
    },
}


def register(app):
    register_tool(MANIFEST)
