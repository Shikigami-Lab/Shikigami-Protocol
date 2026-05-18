"""random_api —— 随机话题来源（默认关闭，可插拔的第五来源）。

两种模式：
  - builtin：从内置题库随机取（破冰题 / 冷知识 / 二选一），无网络。
  - http：GET 配置的 URL，按 JSON 取值路径提取一句话。

拉取到的条目写入 profiles/<id>/topic_random_cache.json（带生成 ID + 时间戳），
resolve 读缓存。builtin 无网络、零成本，每次缺候选即时补；http 的同步请求会阻塞
事件循环，故放到后台线程拉取、写缓存，本轮先用旧缓存，下个自省周期再取到新内容。
"""
import asyncio
import json
import logging
import os
import random
import time
import urllib.request
from typing import Optional

from src.core.topics.base import (TopicCandidate, TopicMaterial, TopicSource,
                                  TopicSourceContext, topic_id_hash)
from src.core.topics.registry import register_topic_source

logger = logging.getLogger(__name__)

_CACHE_FILE = "topic_random_cache.json"
_REFRESH_SECS = 3600          # http 模式：距上次拉取超过此秒数才重新请求
_MAX_CACHED = 6               # 缓存条目上限
_HTTP_TIMEOUT = 3.0

_BUILTIN_POOLS = {
    "icebreaker": [
        "如果今天能多出三个小时，你最想拿来做什么？",
        "最近有没有什么小事让你觉得「今天还不错」？",
        "你最近一次开怀大笑是因为什么？",
        "如果可以瞬间学会一项技能，你会选什么？",
        "你理想中一个完全放空的周末是什么样子？",
        "最近有什么东西特别想买，又一直没下手？",
    ],
    "trivia": [
        "章鱼有三颗心脏，其中两颗在它游泳时会停跳。",
        "蜂蜜几乎不会变质，考古学家挖出过三千年前仍可食用的蜂蜜。",
        "香蕉在植物学上算浆果，而草莓不算。",
        "人的鼻子能区分大约一万亿种不同气味。",
        "土星的密度比水还小，理论上能浮在足够大的水面上。",
        "猫几乎尝不出甜味，它们缺少感知甜的味觉受体。",
    ],
    "would_you_rather": [
        "你会选永远不用睡觉，还是永远不用吃饭？",
        "你会选能飞，还是能隐身？",
        "你会选回到十年前，还是直接跳到十年后？",
        "你会选一直是夏天，还是一直是冬天？",
        "你会选读懂所有动物的话，还是会说所有人类的语言？",
        "你会选记住读过的每一本书，还是记住去过的每一个地方？",
    ],
}


@register_topic_source
class RandomApiTopicSource(TopicSource):
    source_id = "random_api"
    label = "随机话题 API"
    label_en = "Random Topic API"
    enabled_by_default = False
    framing_hint = ("用“突然想问你”“忽然有点好奇”这种玩心十足的口吻抛出这个话题，"
                    "轻松、不正经一点也无妨。")
    framing_hint_en = ("Throw this out playfully — 'random question for you...' — "
                       "keep it light and fun.")

    # ── cache helpers（按 storage_root 操作，后台线程也能调用）────────────────
    def _cache_path(self, storage_root: str) -> str:
        return os.path.join(storage_root, _CACHE_FILE)

    def _load_cache(self, storage_root: str) -> dict:
        try:
            with open(self._cache_path(storage_root), encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("items", {})
            data.setdefault("last_fetch_at", 0.0)
            return data
        except Exception:
            return {"items": {}, "last_fetch_at": 0.0}

    def _save_cache(self, storage_root: str, cache: dict) -> None:
        path = self._cache_path(storage_root)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.debug("[topics.random_api] 缓存写入失败: %s", e)

    def _cache_add(self, storage_root: str, text: str) -> str:
        """把一条话题写入缓存（带生成 ID），裁剪到 _MAX_CACHED，返回 item_id。"""
        cache = self._load_cache(storage_root)
        now = time.time()
        iid = topic_id_hash(text + str(now))
        items = cache["items"]
        items[iid] = {"text": text, "fetched_at": now}
        cache["last_fetch_at"] = now
        if len(items) > _MAX_CACHED:
            oldest = sorted(items.items(), key=lambda kv: kv[1].get("fetched_at", 0))
            for old_id, _ in oldest[:len(items) - _MAX_CACHED]:
                items.pop(old_id, None)
        self._save_cache(storage_root, cache)
        return iid

    # ── fetch ────────────────────────────────────────────────────────────────
    def _fetch_builtin(self, src_cfg: dict) -> str:
        """从内置题库随机取一条（本地、零成本）。"""
        pool_name = (src_cfg.get("builtin_pool") or "icebreaker").strip()
        pool = _BUILTIN_POOLS.get(pool_name) or _BUILTIN_POOLS["icebreaker"]
        return random.choice(pool)

    def _schedule_http_fetch(self, storage_root: str, src_cfg: dict) -> None:
        """把同步 http 拉取丢到后台线程，避免阻塞自省所在的事件循环。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 不在异步上下文，跳过（http 候选下次再补）

        def _work():
            text = self._fetch_http(src_cfg)
            if text:
                self._cache_add(storage_root, text)

        loop.run_in_executor(None, _work)

    def _fetch_http(self, src_cfg: dict) -> Optional[str]:
        url = (src_cfg.get("http_url") or "").strip()
        if not url:
            return None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Shikigami/1.0"})
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
        except Exception as e:
            logger.debug("[topics.random_api] http 拉取失败: %s", e)
            return None
        path = (src_cfg.get("http_json_path") or "").strip()
        text = _extract_json_path(data, path) if path else data
        if isinstance(text, str) and text.strip():
            return text.strip()
        logger.debug("[topics.random_api] http 返回无法按路径取到字符串: path=%r", path)
        return None

    # ── TopicSource API ──────────────────────────────────────────────────────
    def get_candidates(self, ctx: TopicSourceContext) -> list[TopicCandidate]:
        src_cfg = ((ctx.config.get("sources") or {}).get("random_api") or {})
        storage_root = ctx.storage_root
        cache = self._load_cache(storage_root)
        items: dict = cache["items"]
        usable = [iid for iid in items if f"random_api:{iid}" not in ctx.recently_used]

        if (src_cfg.get("mode") or "builtin").strip() == "http":
            # http：同步请求会阻塞事件循环 —— 缺候选或缓存过期时后台线程补，本轮先用旧缓存
            stale = (time.time() - cache.get("last_fetch_at", 0.0)) > _REFRESH_SECS
            if not usable or stale:
                self._schedule_http_fetch(storage_root, src_cfg)
        elif not usable:
            # builtin：本地题库零成本，缺候选就即时补一条
            iid = self._cache_add(storage_root, self._fetch_builtin(src_cfg))
            items = self._load_cache(storage_root)["items"]
            usable.append(iid)

        out: list[TopicCandidate] = []
        for iid in usable[:2]:
            text = ((items.get(iid) or {}).get("text") or "").strip()
            if text:
                summary = text[:60] + ("…" if len(text) > 60 else "")
                out.append(TopicCandidate("random_api", iid, summary))
        return out

    def resolve(self, item_id: str, ctx: TopicSourceContext) -> Optional[TopicMaterial]:
        entry = self._load_cache(ctx.storage_root)["items"].get(item_id)
        text = (entry.get("text") or "").strip() if entry else ""
        if not text:
            return None
        return TopicMaterial("random_api", item_id, text)

    def mark_used(self, item_id: str, ctx: TopicSourceContext) -> None:
        cache = self._load_cache(ctx.storage_root)
        if cache["items"].pop(item_id, None) is not None:
            self._save_cache(ctx.storage_root, cache)


def _extract_json_path(data, path: str):
    """按点分路径从 JSON 取值，支持 list 下标。如 'slip.advice'、'0.text'。"""
    cur = data
    for part in path.split("."):
        part = part.strip()
        if part == "":
            continue
        try:
            if isinstance(cur, list):
                cur = cur[int(part)]
            elif isinstance(cur, dict):
                cur = cur[part]
            else:
                return None
        except (KeyError, IndexError, ValueError, TypeError):
            return None
    return cur
