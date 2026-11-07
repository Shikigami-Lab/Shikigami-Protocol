"""WebSearch 执行层 — 标准化搜索结果，供命令和 Harness 调用。

数据源优先级：
  1. Serper.dev（需 WEBSEARCH_SERPER_API_KEY，结果最好）
  2. 免费搜索（ddgs 包；DuckDuckGo → Yahoo → Yandex，每路最多一次）

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
from src.tools.websearch.log_safety import install_search_log_filter

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0  # seconds
_FREE_TIMEOUT = 6.0  # Keep three free attempts plus Serper within the MCP timeout.
_SERPER_URL = "https://google.serper.dev/search"


class SearchError(RuntimeError):
    """A provider failed; its absence of results is not a successful empty search."""


async def search(query: str, max_results: int = 5, *, raise_on_error: bool = False) -> List[Dict]:
    """Return normalized results, preserving legacy silent failures by default.

    With raise_on_error=True, a successful empty Serper response is final. A
    A failed Serper request falls back to the bounded free providers. If all
    fail, raise SearchError. A successful response (including []) resolves it.
    """
    serper_key = os.getenv("WEBSEARCH_SERPER_API_KEY", "").strip()
    if serper_key:
        if raise_on_error:
            try:
                return await _search_serper(query, serper_key, max_results, raise_on_error=True)
            except SearchError:
                pass
        else:
            results = await _search_serper(query, serper_key, max_results)
            if results:
                return results
        logger.warning("[websearch] Serper 失败，回退 DuckDuckGo")

    if raise_on_error:
        return await _search_ddg(query, max_results, raise_on_error=True)
    return await _search_ddg(query, max_results)


async def _search_serper(
    query: str, api_key: str, max_results: int, *, raise_on_error: bool = False,
) -> List[Dict]:
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

        if raise_on_error and isinstance(data, dict) and "error" in data:
            raise ValueError("Serper returned an error response")
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
        if raise_on_error:
            raise SearchError("Serper search failed") from e
        return []


async def _search_ddg(query: str, max_results: int, *, raise_on_error: bool = False) -> List[Dict]:
    """Bounded free-provider fallback using the existing DDGS library and proxy.

    A provider challenge/transport failure is not a successful empty result.
    Never solve challenges or repeatedly retry the same provider. Each of the
    three endpoints is attempted at most once, within the existing MCP timeout.
    """
    log_tool_websearch_request(query, "ddg", max_results)
    t0 = time.monotonic()
    try:
        from ddgs import DDGS
        from ddgs.engines import ENGINES
        loop = asyncio.get_event_loop()
        proxy = next((value for key in ("DDGS_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
                      if (value := os.getenv(key, "").strip())), None)

        # DDGS silently selects auto for unknown/disabled names. Check the
        # actual registry so a bounded fallback cannot expand to every engine.
        backends = [name for name in ('duckduckgo', 'yahoo', 'yandex') if name in ENGINES.get('text', {})]
        if not backends:
            raise SearchError('No supported free search provider is installed')

        with DDGS(proxy=proxy, timeout=_FREE_TIMEOUT) as ddgs:
            install_search_log_filter()
            for index, backend in enumerate(backends):
                # Cancellation cannot stop an already submitted HTTP request,
                # but must prevent scheduling another provider afterwards.
                await asyncio.sleep(0)
                try:
                    raw = await asyncio.wait_for(loop.run_in_executor(None,
                        lambda name=backend: list(ddgs.text(query, max_results=max_results, backend=name))),
                        timeout=_FREE_TIMEOUT)
                    break
                except Exception as error:
                    # DDGS uses the same "No results found" exception for
                    # empty pages and challenges. It is not proof of zero hits.
                    if index + 1 == len(backends):
                        raise
                    next_backend = backends[index + 1]
                    logger.warning('%s unavailable (%s); trying %s once', backend, type(error).__name__, next_backend)
                    log_tool_websearch_request(query, next_backend, max_results)
        results = [
            {
                "title":   r.get("title", ""),
                "snippet": r.get("body", ""),
                "url":     r.get("href", ""),
            }
            for r in raw
            if r.get("title")
        ]
        if raise_on_error and raw and not results:
            raise ValueError("Free search returned no usable result rows")
        duration_ms = (time.monotonic() - t0) * 1000
        logger.debug("[websearch] %s 返回 %d 条 query=%s", backend, len(results), query)
        log_tool_websearch_result(query, backend, len(results), results, duration_ms)
        return results
    except ImportError:
        logger.warning("[websearch] ddgs 包未安装，请 pip install ddgs")
        if raise_on_error:
            raise SearchError("DuckDuckGo search dependency is unavailable") from None
        return []
    except Exception as e:
        duration_ms = (time.monotonic() - t0) * 1000
        # Transport errors may echo an authenticated proxy URL or its password.
        # Retain the failure category, never raw exception text or chained traces.
        error = f"Free web search failed ({type(e).__name__})"
        logger.warning("[websearch] %s", error)
        log_tool_websearch_result(query, "ddg", 0, [], duration_ms, error=error)
        if raise_on_error:
            raise SearchError("Free web search sources are unavailable") from None
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
