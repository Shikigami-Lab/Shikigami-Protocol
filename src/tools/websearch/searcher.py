"""WebSearch 执行层 — 标准化搜索结果，供 command / TrendFetcher 调用。

数据源优先级：
  1. Serper.dev（需 WEBSEARCH_SERPER_API_KEY，结果最好）
  2. DuckDuckGo（ddgs 包，免费无 key，真实网页搜索）

返回格式：List[{title, snippet, url}]
调用方不感知数据源来自哪里。
"""
import asyncio
import logging
import os
import time
from typing import List, Dict

import httpx

from src.utils.debug_logger import log_tool_websearch_request, log_tool_websearch_result

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0  # seconds
_SERPER_URL = "https://google.serper.dev/search"


async def search(query: str, max_results: int = 5) -> List[Dict]:
    """执行搜索，返回标准化结果列表。失败时返回空列表（静默）。"""
    serper_key = os.getenv("WEBSEARCH_SERPER_API_KEY", "").strip()
    if serper_key:
        results = await _search_serper(query, serper_key, max_results)
        if results:
            return results
        logger.warning("[websearch] Serper 失败，回退 DuckDuckGo")

    return await _search_ddg(query, max_results)


async def _search_serper(query: str, api_key: str, max_results: int) -> List[Dict]:
    log_tool_websearch_request(query, "serper", max_results)
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _SERPER_URL,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": query, "num": max_results},
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for item in data.get("organic", [])[:max_results]:
            results.append({
                "title":   item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "url":     item.get("link", ""),
            })
        duration_ms = (time.monotonic() - t0) * 1000
        logger.debug("[websearch] Serper 返回 %d 条 query=%s", len(results), query)
        log_tool_websearch_result(query, "serper", len(results), results, duration_ms)
        return results
    except Exception as e:
        duration_ms = (time.monotonic() - t0) * 1000
        logger.warning("[websearch] Serper 异常: %s", e)
        log_tool_websearch_result(query, "serper", 0, [], duration_ms, error=str(e))
        return []


async def _search_ddg(query: str, max_results: int) -> List[Dict]:
    """使用 ddgs 包进行真实网页搜索（非 Instant Answer API）。"""
    log_tool_websearch_request(query, "ddg", max_results)
    t0 = time.monotonic()
    try:
        from ddgs import DDGS
        loop = asyncio.get_event_loop()

        def _run():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results))

        raw = await loop.run_in_executor(None, _run)
        results = [
            {
                "title":   r.get("title", ""),
                "snippet": r.get("body", ""),
                "url":     r.get("href", ""),
            }
            for r in raw
            if r.get("title")
        ]
        duration_ms = (time.monotonic() - t0) * 1000
        logger.debug("[websearch] DDG 返回 %d 条 query=%s", len(results), query)
        log_tool_websearch_result(query, "ddg", len(results), results, duration_ms)
        return results
    except ImportError:
        logger.warning("[websearch] ddgs 包未安装，请 pip install ddgs")
        return []
    except Exception as e:
        duration_ms = (time.monotonic() - t0) * 1000
        logger.warning("[websearch] DuckDuckGo 异常: %s", e)
        log_tool_websearch_result(query, "ddg", 0, [], duration_ms, error=str(e))
        return []


def format_for_llm(results: List[Dict], query: str) -> str:
    """把搜索结果格式化成注入 context_for_llm 的文本。"""
    if not results:
        return f"[搜索结果]\n未找到关于「{query}」的相关内容。"
    lines = [f"[搜索结果：{query}]"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "").strip()
        snippet = r.get("snippet", "").strip()[:400]
        url = r.get("url", "").strip()
        if title and snippet and title.lower() != snippet.lower()[:len(title)].lower():
            lines.append(f"{i}. {title}\n   {snippet}")
        else:
            lines.append(f"{i}. {snippet or title}")
        if url:
            lines.append(f"   {url}")
    return "\n".join(lines)
