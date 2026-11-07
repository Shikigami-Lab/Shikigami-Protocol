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
        "你觉得哪种锅盖最像一顶帽子？",
        "一颗弹珠滚出画面后，你觉得它去了哪儿？",
        "哪种饼干掰开时的声音最解压？",
        "给月亮寄一张明信片，你会画什么？",
        "什么样的门，一看就让人好奇后面是什么？",
        "如果图书馆有个只收怪书名的书架，你会放上哪本？",
        "汤里的最后一口，你一般留给什么？",
        "给纸飞机画个机徽，你会画成什么样？",
        "哪种街边招牌，光看字形就猜得到卖什么？",
        "下雨时，哪种地面上的倒影最好看？",
        "如果给一双总走散的袜子拍动画，第一集讲什么？",
        "哪种水果切开之后最像一幅画？",
        "你觉得哪种动物最适合当裁判？",
        "用三样文具搭一座微型游乐场，你会怎么搭？",
        "哪种食物放凉了反而更好吃？",
        "你会给一台总迷路的扫地机器人起什么外号？",
        "要挑一块石头当镇纸，你会挑什么样的？",
        "有没有哪个字，念起来就像它说的那个动作？",
        "如果影子能从口袋里掏出一样东西，你猜是什么？",
        "什么颜色的信封最让人想拆开看看？",
        "逛面包店时，你通常第一眼会看向哪一格？",
        "给一座广场添一尊搞怪雕像，你想让它做什么动作？",
        "哪样日用品一放大就会变得特别滑稽？",
        "一只鸭子要走红毯，哪种走法最有派头？",
        "拼图里你最爱先拼哪一块？",
        "如果树叶能盖章，哪种叶子印出来最好看？",
        "什么东西摸起来比看起来有意思？",
        "要给一场纸杯叠塔比赛加条搞怪规则，你会加什么？",
        "哪个动画音效最适合用来形容踩进水坑？",
        "哪样东西光看包装，就让你猜不出里面装了什么？",
    ],
    "trivia": [
        "章鱼有三颗心脏，其中两颗在它游泳时会停跳。",
        "蜂蜜几乎不会变质，考古学家挖出过三千年前仍可食用的蜂蜜。",
        "香蕉在植物学上算浆果，而草莓不算。",
        "人的鼻子能区分大约一万亿种不同气味。",
        "土星的密度比水还小，理论上能浮在足够大的水面上。",
        "猫几乎尝不出甜味，它们缺少感知甜的味觉受体。",
        "火星上也有蓝色的日落，好奇号就拍到过太阳周围泛蓝的天空。",
        "激光测距发现，月球和地球的平均距离每年增加约 3.8 厘米。",
        "金星自转一圈约要 243 个地球日，比它绕太阳一圈还久。",
        "火烈鸟羽毛的粉色来自食物里的类胡萝卜素，藻类和小甲壳动物都能提供这种色素。",
        "裸鼻袋熊的便便像小方块，棱角是在肠道里慢慢成形的。",
        "北美的林蛙能在体内部分结冰的状态下过冬，等天气转暖再解冻活动。",
        "蜜蜂会跳摇摆舞，告诉同伴花丛在哪个方向、离蜂巢大概多远。",
        "鹦嘴鱼在礁石上觅食时会吞下珊瑚碎屑，磨碎后排出，成了夏威夷一些海滩上的白沙。",
        "声音在海水里通常每秒能跑约 1500 米，速度是空气中的四倍多。",
        "大西洋上的马尾藻海没有陆地作边界，四周是洋流。",
        "地球表面离地心最远的地方是厄瓜多尔的钦博拉索山顶，因为地球在赤道附近向外鼓起。",
        "有些浮石里满是气孔，密度小到能浮在水面上。",
        "世界上最深的湖是西伯利亚的贝加尔湖。",
        "木星自转一圈还不到 10 小时，绕太阳一圈却要大约 12 个地球年。",
        "日语平假名「あ」是从汉字「安」的草书写法演变来的。",
        "韩文会把同一个音节的字母拼成一个方块，比如「한」是由「ㅎ」「ㅏ」「ㄴ」拼成的。",
        "emoji 这个词来自日语「絵文字」，「絵」是图画，「文字」就是字符。",
        "浮在水面上的冰山，体积大部分藏在水下。",
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
        "吃面时，你站细面挂汤派，还是宽面裹酱派？",
        "借你一把魔法伞，想要能接住落叶的，还是能抖出彩虹的？",
        "进一间旧玩具店，先拧会翻跟头的发条鸭，还是先试会转弯的陀螺？",
        "捡到一枚空白徽章，你想给它刻一张鬼脸，还是刻一个假门牌号？",
        "来一杯会变颜色的汽水，还是一块切开才知道口味的蛋糕？",
        "路口有两条魔法斑马线，你走踩着会发光的，还是踩着会响的？",
        "吃烤红薯，你先挖中间的软心，还是先掰边上的焦皮？",
        "选一颗随身印章，你要能盖出会动的小鱼，还是会展开的花？",
        "逛跳蚤市场只带走一件，你挑走得飞快的沙漏，还是照什么都成圆形的镜子？",
        "一串钥匙只能挂件小东西，你挂一把迷你尺，还是一枚能转的风车？",
        "周围的字能变一次，你想让路牌变成谜语，还是让菜单变成漫画？",
        "你宁愿开水壶烧开时吹一声口哨，还是烤面包机弹起来时敲一声小锣？",
        "只剩一张纸，你拿来折一只青蛙，还是画一张藏宝图？",
        "二选一，你想看海獭打保龄球，还是看水豚坐旋转木马？",
        "你想试试用长柄勺画沙画，还是用滚轮在雪上压花纹？",
        "坐旋转餐厅，你希望窗外转，还是希望菜自己慢慢转到面前？",
        "做热三明治，你选压出焦脆条纹，还是烤到表面鼓起来？",
        "袖口只能附一种魔法，你要能卷成小篮子，还是能展开当桌布？",
        "想试试在巨型枕头上蹦，还是在巨型滑梯上滑？",
        "两台留声机都只放一种声音，你听冰裂开的轻响，还是石子在罐里打转？",
        "逛无重力展厅，你想甩出一串悬空水珠，还是把纸带抛成一条螺旋？",
        "收一枚奇怪的冰箱贴，你选会绕着冰箱门跑的火车，还是会从冰箱门探头的鼹鼠？",
        "你愿意把鞋带换成轻轻一拍就系好的，还是把拉链换成捏一下就拉好的？",
        "两份早餐里，你挑一叠能抹果酱的薄饼，还是一碗能撒脆片的酸奶？",
        "桌游里能多一件道具，你选会自己滚回来的骰子，还是会自己站起来的棋子？",
        "两种古怪果树，你想摘长成星形的梨，还是剥开有条纹的橘子？",
        "要在玻璃罐里收一样东西，你装篝火的火星，还是装破晓前的微光？",
        "盲选一台扭蛋机，你投专出微缩家具的那台，还是专出迷你乐器的那台？",
        "玩一局投掷游戏，你想往会滚的桶里投球，还是往会摇晃的木桩上套圈？",
        "选一个会变身的文具盒，你要打开就变画板的，还是摊开就变棋盘的？",
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
