"""TopicSource 抽象层 —— 主动话题发现的来源抽象。

每个来源实现两件事：
  - get_candidates(ctx)  自省阶段：产出带稳定 ID 的候选话题
  - resolve(item_id,ctx) ASE 阶段：按 ID 拉取完整新鲜素材

所有来源调用均 best-effort：单个来源抛异常不应影响其他来源，
也不应崩掉自省 / ASE 循环（由调用方包 try/except）。
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


def topic_id_hash(text: str) -> str:
    """为没有天然稳定 ID 的素材（如记忆事实）生成短 ID。"""
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()[:10]


@dataclass
class TopicCandidate:
    """给自省 LLM 挑选用的候选话题（只含短描述，不含完整素材）。"""
    source_id: str
    item_id: str
    summary: str

    @property
    def ref(self) -> str:
        """复合 ID，形如 'trend:a3f1'，自省 LLM 的 topic_pick 即填此值。"""
        return f"{self.source_id}:{self.item_id}"


@dataclass
class TopicMaterial:
    """给 ASE 演出用的完整话题素材。"""
    source_id: str
    item_id: str
    material: str
    meta: dict = field(default_factory=dict)


@dataclass
class TopicSourceContext:
    """传给 TopicSource 各方法的上下文。"""
    profile_id: str
    storage_root: str
    profile: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)        # topic_discovery 配置
    recently_used: set = field(default_factory=set)   # {"source:item_id"} 最近用过的
    reflection_thought: str = ""                      # 自省最近的 thought（ai_self 用）
    emotion: Optional[dict] = None                    # emotion_state（ai_self 用）
    memory_manager: Any = None                        # MemoryManager（user_life 用）
    session: Any = None
    app: Any = None


class TopicSource(ABC):
    """所有话题来源的抽象基类。"""

    source_id: str = ""
    label: str = ""
    label_en: str = ""
    enabled_by_default: bool = True

    # 来源级、静态的语气指引 —— 注入 ASE prompt，决定 AI 用什么口吻引出该来源的话题
    framing_hint: str = ""
    framing_hint_en: str = ""

    @abstractmethod
    def get_candidates(self, ctx: TopicSourceContext) -> list[TopicCandidate]:
        """返回候选话题列表（已排除 ctx.recently_used）。无则返回空列表。"""
        ...

    @abstractmethod
    def resolve(self, item_id: str, ctx: TopicSourceContext) -> Optional[TopicMaterial]:
        """按 item_id 拉取完整素材。条目不存在 / 已失效时返回 None。"""
        ...

    def mark_used(self, item_id: str, ctx: TopicSourceContext) -> None:
        """ASE 发完该话题后调用。默认空实现；需要来源内去重的来源覆盖此方法。"""
        return None

    def get_framing_hint(self, locale: str = "zh") -> str:
        if locale == "en":
            return self.framing_hint_en or self.framing_hint
        return self.framing_hint
