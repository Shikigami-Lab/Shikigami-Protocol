"""CadenceTracker — 追踪用户回复节奏，供 time_context segment 和 ReflectionEngine 使用。

纯内存，服务重启后重置（可接受；数据很快重建）。
不是 PromptSegment，是 app.state 级数据工具。
"""
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_MAX_HISTORY = 20   # 每个 profile 保留最近20条时间戳


@dataclass
class CadenceSignals:
    has_data: bool = False
    recent_count_10min: int = 0       # 最近10分钟消息数
    avg_gap_min: float = 0.0          # 最近5个回复间隔的平均值（分钟）
    last_gap_min: float = 0.0         # 距上条消息的时间（分钟）
    is_quick: bool = False            # 快速交流（avg_gap < 1min，3+条）
    is_slow: bool = False             # 节奏偏慢（avg_gap > 10min，有足够历史）


class CadenceTracker:
    def __init__(self):
        self._ts: dict = defaultdict(lambda: deque(maxlen=_MAX_HISTORY))

    def record(self, profile_id: str, ts: Optional[float] = None) -> None:
        """记录一条用户消息时间戳。"""
        self._ts[profile_id].append(ts or time.time())
        logger.debug("[CadenceTracker] recorded profile=%s", profile_id)

    def get_signals(self, profile_id: str) -> CadenceSignals:
        """计算并返回当前节奏信号。"""
        ts_list = list(self._ts[profile_id])
        if len(ts_list) < 2:
            return CadenceSignals()

        now = time.time()
        recent_count = sum(1 for t in ts_list if now - t < 600)
        gaps_s = [ts_list[i] - ts_list[i - 1] for i in range(1, len(ts_list))]
        recent_gaps = gaps_s[-5:] if gaps_s else []
        avg_gap_s = sum(recent_gaps) / len(recent_gaps) if recent_gaps else 0.0
        last_gap_s = now - ts_list[-1]

        return CadenceSignals(
            has_data=True,
            recent_count_10min=recent_count,
            avg_gap_min=round(avg_gap_s / 60, 1),
            last_gap_min=round(last_gap_s / 60, 1),
            is_quick=avg_gap_s < 60 and recent_count >= 3,
            is_slow=avg_gap_s > 600 and len(ts_list) >= 3,
        )
