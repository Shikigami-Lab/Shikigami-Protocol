"""random_api —— 随机话题来源（默认关闭，可插拔的第五来源）。

两种模式：
  - builtin：从内置题库随机取（破冰题 / 冷知识 / 二选一），无网络。
  - http：GET 配置的 URL，按 JSON 取值路径提取一句话。

拉取到的条目写入 profiles/<id>/topic_random_cache.json（带生成 ID + 时间戳），
resolve 读缓存。builtin 无网络、零成本，每次缺候选即时补；http 的同步请求会阻塞
事件循环，故放到后台线程拉取、写缓存，本轮先用旧缓存，下个自省周期再取到新内容。
已用话题正文保存在同一缓存中，按 recent_used_window 去重；内置小题库至少保留一条可抽。
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
        "今天有没有一个瞬间，你想把它偷偷存档？",
        "最近有没有一首歌，听到前奏就不想切走？",
        "要给现在的心情配一种天气，你会选哪种？",
        "如果今晚只做一件让自己舒服的事，你想做什么？",
        "有没有一种味道，会让你一下子想起某个地方？",
        "最近你对什么东西突然有了点好奇？",
        "如果把今天剪成一分钟短片，你会留下哪一幕？",
        "有没有一件看起来很普通、你却很喜欢的小东西？",
        "如果我们开一家没什么正经生意的小店，你想卖什么？",
        "有什么事情你不擅长，但做起来还是挺开心？",
        "如果给你的房间加一个完全没必要的功能，你想加什么？",
        "你有什么别人觉得奇怪、自己却很享受的小习惯？",
        "最近看到过哪句话，想多停留一会儿？",
        "你有没有一个想去看看、暂时还没计划的地方？",
        "如果能重新体验某个游戏、电影或故事的第一次，你会选哪个？",
        "有什么小技能，虽然派不上大用场，你还是很想学？",
        "如果给今天起个名字，你觉得它叫什么？",
        "你喜欢怎样的背景声音：雨声、咖啡馆，还是安静一点？",
        "有没有一种小时候喜欢的东西，现在还会让你开心？",
        "如果突然收到一张不用赶路的车票，你想在哪里下车？",
        "要给现在的自己准备一个小礼物，你会选什么？",
        "你有没有特别喜欢的一段路，哪怕它没什么风景？",
        "有什么东西你愿意花时间慢慢做，完全不追求效率？",
        "如果今天可以删掉一个小麻烦，你想删掉哪个？",
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
        "你会选一间能看海的房间，还是一间能听雨的房间？",
        "你会选每天多一小时独处，还是每周多一个完全自由的下午？",
        "你会选随时把东西找到，还是让所有设备永远不用充电？",
        "你会选在熟悉的城市发现秘密小巷，还是去陌生的小镇住一周？",
        "你会选有一只会聊天的猫，还是一盆会吐槽的植物？",
        "你会选每次做饭都刚刚好，还是每次拍照都拍到想要的样子？",
        "你会选把梦录下来，还是把脑中的画面直接画出来？",
        "你会选一辆开往任意海边的车，还是一扇通往任意书店的门？",
        "你会选生活里自带背景音乐，还是偶尔出现只有你看得见的字幕？",
        "你会选把房间变成森林小屋，还是变成宇宙飞船的休息舱？",
        "你会选随时暂停十分钟，还是每天撤回一句刚说出口的话？",
        "你会选和喜欢的作者吃顿饭，还是进入喜欢的故事里待一天？",
        "你会选每周试一种没吃过的食物，还是每月学一种没碰过的爱好？",
        "你会选随时知道植物缺什么，还是随时知道宠物在想什么？",
        "你会选窗外永远是喜欢的风景，还是门外总能遇到有趣的小店？",
        "你会选把讨厌的家务瞬间做完，还是让所有排队都只等一分钟？",
        "你会选去过去看看一座城市，还是去未来看看一片森林？",
        "你会选一整天没人打扰，还是有人陪你漫无目的地逛一天？",
        "你会选做东西时灵感不断，还是想休息时马上就能放松？",
        "你会选拥有随时变得安静的耳机，还是随时能变舒服的椅子？",
        "你会选一次非常惊喜的旅行，还是十次熟悉又舒服的小出门？",
        "你会选亲手做一份小礼物，还是一起完成一件没做过的小事？",
        "你会选在山顶看日出，还是在海边等日落？",
        "你会选家里有一间秘密图书馆，还是有一个四季都能用的小花园？",
    ],
}


def _builtin_pool(src_cfg: dict) -> list[str]:
    pool_name = (src_cfg.get("builtin_pool") or "icebreaker").strip()
    return _BUILTIN_POOLS.get(pool_name) or _BUILTIN_POOLS["icebreaker"]


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
            data.setdefault("recent_texts", [])
            return data
        except Exception:
            return {"items": {}, "last_fetch_at": 0.0, "recent_texts": []}

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
    def _fetch_builtin(self, src_cfg: dict, recent_texts: Optional[list[str]] = None) -> str:
        """从内置题库随机取一条，避开近期已经使用的正文。"""
        pool = _builtin_pool(src_cfg)
        available = [text for text in pool if text not in (recent_texts or [])]
        return random.choice(available or pool)

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
        mode = (src_cfg.get("mode") or "builtin").strip()
        window = max(0, int(ctx.config.get("recent_used_window", 10)))
        recent_texts = cache["recent_texts"]
        pool = None if mode == "http" else _builtin_pool(src_cfg)
        if pool is not None:
            recent_texts = [text for text in recent_texts if text in pool]
            window = min(window, len(pool) - 1)
        recent_texts = recent_texts[-window:] if window else []
        usable = [
            iid for iid, entry in items.items()
            if f"random_api:{iid}" not in ctx.recently_used
            and (entry.get("text") or "").strip() not in recent_texts
            and (pool is None or (entry.get("text") or "").strip() in pool)
        ]

        if mode == "http":
            # http：同步请求会阻塞事件循环 —— 缺候选或缓存过期时后台线程补，本轮先用旧缓存
            stale = (time.time() - cache.get("last_fetch_at", 0.0)) > _REFRESH_SECS
            if not usable or stale:
                self._schedule_http_fetch(storage_root, src_cfg)
        elif not usable:
            # builtin：本地题库零成本，缺候选就即时补一条
            iid = self._cache_add(storage_root, self._fetch_builtin(src_cfg, recent_texts))
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
        entry = cache["items"].pop(item_id, None)
        if entry is not None:
            text = (entry.get("text") or "").strip()
            window = max(0, int(ctx.config.get("recent_used_window", 10)))
            recent_texts = [previous for previous in cache["recent_texts"] if previous != text]
            if text:
                recent_texts.append(text)
            cache["recent_texts"] = recent_texts[-window:] if window else []
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
