"""内置随机灵感。保留 random_api 来源 ID，以兼容现有设置和话题引用。"""
import json
import logging
import os
import random
import time
from typing import Optional

from src.core.topics.base import (TopicCandidate, TopicMaterial, TopicSource,
                                  TopicSourceContext, topic_id_hash)
from src.core.topics.registry import register_topic_source

logger = logging.getLogger(__name__)

_CACHE_FILE = "topic_random_cache.json"
_MAX_CACHED = 6

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


def _builtin_pool(ctx: TopicSourceContext) -> list[str]:
    config = (ctx.config.get("sources") or {}).get("random_api") or {}
    pool_name = (config.get("builtin_pool") or "icebreaker").strip()
    return _BUILTIN_POOLS.get(pool_name) or _BUILTIN_POOLS["icebreaker"]


@register_topic_source
class RandomApiTopicSource(TopicSource):
    source_id = "random_api"
    label = "本地随机灵感"
    label_en = "Local Random Inspiration"
    enabled_by_default = False
    framing_hint = ("用“突然想问你”“忽然有点好奇”这种玩心十足的口吻抛出这个话题，"
                    "轻松、不正经一点也无妨。")
    framing_hint_en = ("Throw this out playfully — 'random question for you...' — "
                       "keep it light and fun.")

    def _cache_path(self, storage_root: str) -> str:
        return os.path.join(storage_root, _CACHE_FILE)

    def _load_cache(self, storage_root: str) -> dict:
        try:
            with open(self._cache_path(storage_root), encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("items", {})
            data.setdefault("recent_texts", [])
            return data
        except Exception:
            return {"items": {}, "recent_texts": []}

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

    def _cache_add(self, storage_root: str, text: str, pool: list[str]) -> str:
        """把一条话题写入缓存（带生成 ID），裁剪到 _MAX_CACHED，返回 item_id。"""
        cache = self._load_cache(storage_root)
        now = time.time()
        iid = topic_id_hash(text + str(now))
        items = cache["items"]
        items[iid] = {"text": text, "fetched_at": now}
        # Bound local candidates while preserving older entries outside the pool.
        pool_items = [(item_id, entry) for item_id, entry in items.items() if entry.get("text") in pool]
        pool_items.sort(key=lambda pair: pair[1].get("fetched_at", 0), reverse=True)
        for old_id, _ in pool_items[_MAX_CACHED:]:
            items.pop(old_id)
        self._save_cache(storage_root, cache)
        return iid

    # ── TopicSource API ──────────────────────────────────────────────────────
    def get_candidates(self, ctx: TopicSourceContext) -> list[TopicCandidate]:
        storage_root = ctx.storage_root
        cache = self._load_cache(storage_root)
        items: dict = cache["items"]
        pool = _builtin_pool(ctx)
        window = min(max(0, int(ctx.config.get("recent_used_window", 10))), len(pool) - 1)
        recent_texts = [text for text in cache["recent_texts"] if text in pool]
        recent_texts = recent_texts[-window:] if window else []
        usable = [
            iid for iid, entry in items.items()
            if f"random_api:{iid}" not in ctx.recently_used
            and (entry.get("text") or "").strip() not in recent_texts
            and (entry.get("text") or "").strip() in pool
        ]

        if not usable:
            text = random.choice([text for text in pool if text not in recent_texts])
            iid = self._cache_add(storage_root, text, pool)
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
        if text not in _builtin_pool(ctx):
            return None
        return TopicMaterial("random_api", item_id, text)

    def mark_used(self, item_id: str, ctx: TopicSourceContext) -> None:
        cache = self._load_cache(ctx.storage_root)
        entry = cache["items"].get(item_id) or {}
        text = (entry.get("text") or "").strip()
        if text in _builtin_pool(ctx):
            cache["items"].pop(item_id)
            window = max(0, int(ctx.config.get("recent_used_window", 10)))
            recent_texts = [previous for previous in cache["recent_texts"] if previous != text]
            recent_texts.append(text)
            cache["recent_texts"] = recent_texts[-window:] if window else []
            self._save_cache(ctx.storage_root, cache)
