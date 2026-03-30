"""TrendFetcher — 后台趋势抓取任务。

每个 profile 的 trend_config 独立管理，统一在一个 asyncio 任务中轮询。
失败静默，只记录到 log，不影响主流程。

数据源类型：
  rss       → feedparser + httpx（需 pip install feedparser）
  websearch → WebSearch 工具（src/tools/websearch/searcher.py）
  api       → JSON API，兼容 DailyHotApi / B站热词等格式
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# 每次检查循环的最小休眠（秒），避免所有 profile 同时唤醒
_CHECK_INTERVAL = 60


_RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; FeedFetcher/1.0; +https://github.com/)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}


async def _fetch_rss(url: str, max_items: int = 15) -> List[Dict]:
    """抓取 RSS feed，返回 [{title, snippet, url}]。异常向上抛，由调用方记录到 tools.log。"""
    try:
        import feedparser
    except ImportError:
        raise RuntimeError("feedparser not installed — pip install feedparser")

    import httpx
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        resp = await client.get(url, headers=_RSS_HEADERS)
        resp.raise_for_status()
        status = resp.status_code
        content = resp.text

    loop = asyncio.get_event_loop()
    feed = await loop.run_in_executor(None, feedparser.parse, content)

    if len(feed.entries) == 0:
        # 解析成功但无条目——可能是 HTML 错误页、空 feed 或格式不兼容
        snippet = content[:200].replace("\n", " ")
        raise RuntimeError(f"HTTP {status} OK but feed has 0 entries. Response preview: {snippet!r}")

    items = []
    for entry in feed.entries[:max_items]:
        title = (getattr(entry, "title", "") or "").strip()
        snippet = (getattr(entry, "summary", "") or "").strip()
        link = (getattr(entry, "link", "") or "").strip()
        if title:
            items.append({"title": title, "snippet": snippet[:400], "url": link})
    return items


async def _fetch_websearch(query: str, max_items: int = 5) -> List[Dict]:
    """通过 WebSearch 工具抓取趋势条目。异常向上抛，由调用方记录到 tools.log。"""
    from src.tools.websearch.searcher import search
    return await search(query, max_results=max_items)


async def _fetch_api(url: str, max_items: int = 10) -> List[Dict]:
    """调用 JSON API 获取热榜。异常向上抛，由调用方记录到 tools.log。

    支持的响应格式：
    - DailyHotApi: {code, data: [{title, desc, url}]}
    - B站热词:     {code, list: [{show_name, keyword, heat_score}]}
    """
    import httpx
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        resp = await client.get(url, headers=_RSS_HEADERS)
        resp.raise_for_status()
        data = resp.json()

    # 尝试各种常见的列表字段
    raw = data.get("data") or data.get("items") or data.get("list") or []
    if not raw:
        raise RuntimeError(f"API returned empty list (code={data.get('code')}, keys={list(data.keys())})")

    items = []
    for entry in raw[:max_items]:
        # title：优先 show_name（B站），其次 title/name/keyword
        title = (
            entry.get("show_name") or entry.get("title") or
            entry.get("name") or entry.get("keyword") or ""
        ).strip()
        snippet = (entry.get("desc") or entry.get("description") or entry.get("summary") or "").strip()
        link = (entry.get("url") or entry.get("link") or entry.get("mobileUrl") or "").strip()
        if title:
            items.append({"title": title, "snippet": snippet[:400], "url": link})
    return items


async def run_fetch_once(profile_id: str, storage_root: str, profile_data: Dict[str, Any]) -> int:
    """单次抓取某 profile 的所有趋势源，返回总添加条目数。"""
    from src.tools.trends.store import TrendStore
    from src.utils.debug_logger import log_tool_trend

    trend_cfg = profile_data.get("trend_config") or {}
    sources = trend_cfg.get("sources") or []
    if not sources:
        return 0

    max_cached = int(trend_cfg.get("max_cached_items", 20))
    # 每个源的均等配额：总上限 / 源数，至少 1 条
    per_source_quota = max(1, max_cached // len(sources))
    store = TrendStore(storage_root)
    total_added = 0

    for source in sources:
        src_type = source.get("type", "rss")
        label = source.get("label", src_type)
        src_url = source.get("url", "") or source.get("query", "")
        raw: List[Dict] = []
        src_error = ""

        try:
            if src_type == "rss":
                url = source.get("url", "").strip()
                if url:
                    raw = await _fetch_rss(url, max_items=per_source_quota)
            elif src_type == "websearch":
                query = source.get("query", "").strip()
                if query:
                    raw = await _fetch_websearch(query, max_items=per_source_quota)
            elif src_type == "api":
                url = source.get("url", "").strip()
                if url:
                    raw = await _fetch_api(url, max_items=per_source_quota)
        except Exception as e:
            src_error = str(e)
            logger.warning("[TrendFetcher] source error profile=%s label=%s: %s", profile_id, label, e)

        added = 0
        if raw:
            items = [
                {
                    "title": r.get("title", ""),
                    "snippet": r.get("snippet", ""),
                    "source_label": label,
                    "fetched_at": datetime.now().isoformat(),
                }
                for r in raw
                if r.get("title", "").strip()
            ]
            added = store.add_items(items, max_unused=max_cached, source_quota=per_source_quota)
            total_added += added

        log_tool_trend(
            "source",
            profile_id=profile_id,
            source_label=label,
            source_type=src_type,
            source_url=src_url,
            items_fetched=len(raw),
            items_added=added,
            error=src_error,
        )

    store.prune_old_used()

    log_tool_trend(
        action="fetch_done",
        profile_id=profile_id,
        sources_count=len(sources),
        added_count=total_added,
        total_unused=store.count_unused(),
    )
    logger.info("[TrendFetcher] profile=%s total_added=%d unused=%d", profile_id, total_added, store.count_unused())
    return total_added


async def trend_fetcher_loop(app):
    """主趋势抓取循环。在 server.py 启动时作为 asyncio task 运行。"""
    # per-profile 上次抓取时间
    _last_fetch: Dict[str, float] = {}

    logger.info("[TrendFetcher] loop started")

    while True:
        try:
            sm = getattr(getattr(app, "state", None), "session_manager", None)
            if sm:
                for session in sm.list_sessions():
                    try:
                        profile_path = os.path.join("profiles", f"{session.profile_id}.json")
                        if not os.path.exists(profile_path):
                            continue
                        with open(profile_path, encoding="utf-8") as f:
                            profile_data = json.load(f)

                        trend_cfg = profile_data.get("trend_config") or {}
                        if not trend_cfg.get("enabled", False):
                            continue

                        interval = int(trend_cfg.get("fetch_interval_seconds", 7200))
                        last = _last_fetch.get(session.profile_id, 0.0)
                        if time.time() - last < interval:
                            continue

                        _last_fetch[session.profile_id] = time.time()
                        await run_fetch_once(session.profile_id, session.storage_root, profile_data)

                    except Exception as e:
                        logger.warning("[TrendFetcher] session=%s error: %s", session.id, e)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[TrendFetcher] loop error: %s", e)

        await asyncio.sleep(_CHECK_INTERVAL)
