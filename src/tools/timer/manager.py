"""TimerManager — 内存计时器，线程安全。

全局单例 _timer_manager 供模块内其他文件直接 import。
到期回调在后台线程中调用；调用方自行处理 asyncio 桥接。
"""
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

from src.utils.debug_logger import log_timer, log_tool_timer, log_error


class TimerManager:
    """简单内存计时器，线程安全。"""

    def __init__(self):
        self._timers: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        self._expire_callbacks: List = []   # List[Callable[[dict], None]]

    def register_expire_callback(self, cb) -> None:
        """注册到期回调（在计时线程中调用，需自行处理异步桥）。"""
        self._expire_callbacks.append(cb)

    def start_timer(self, seconds: int, label: str = "计时器", session_id: str = "") -> str:
        timer_id = str(uuid.uuid4())
        end_time = time.time() + max(0, int(seconds))
        entry = {
            "id": timer_id,
            "label": label,
            "seconds": int(seconds),
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "end_timestamp": end_time,
            "running": True,
            "session_id": (session_id or "").strip(),
        }
        with self._lock:
            self._timers[timer_id] = entry
        thread = threading.Thread(target=self._run_timer, args=(timer_id,), daemon=True)
        thread.start()
        logger.info("[TimerManager] 启动 id=%s label=%s seconds=%d", timer_id, label, seconds)
        log_timer("", "start", timer_id=timer_id, label=label, seconds=seconds)
        log_tool_timer("start", timer_id=timer_id, label=label, seconds=int(seconds), session_id=session_id)
        return timer_id

    def stop_timer(self, timer_id: str) -> Optional[Dict]:
        with self._lock:
            if timer_id not in self._timers:
                return None
            info = self._timers.pop(timer_id)
        label = info.get("label", "")
        logger.info("[TimerManager] 停止 id=%s label=%s", timer_id, label)
        log_tool_timer("stop", timer_id=timer_id, label=label,
                       seconds=info.get("seconds", 0),
                       session_id=info.get("session_id", ""),
                       remaining_seconds=max(0, int(info["end_timestamp"] - time.time())))
        return info

    def get_status(self, timer_id: str) -> Optional[Dict]:
        with self._lock:
            info = self._timers.get(timer_id)
            if not info:
                return None
            remaining = max(0, int(info["end_timestamp"] - time.time()))
            log_tool_timer("query", timer_id=timer_id, label=info.get("label", ""),
                           seconds=info.get("seconds", 0),
                           session_id=info.get("session_id", ""),
                           remaining_seconds=remaining)
            return {
                "id": info["id"],
                "label": info["label"],
                "remaining_seconds": remaining,
                "running": info["running"],
            }

    def list_timers(self) -> List[Dict]:
        with self._lock:
            return list(self._timers.values())

    def _run_timer(self, timer_id: str):
        while True:
            with self._lock:
                info = self._timers.get(timer_id)
                if not info or not info.get("running"):
                    return
                remaining = info["end_timestamp"] - time.time()
            if remaining <= 0:
                break
            time.sleep(min(1, max(0.1, remaining)))

        with self._lock:
            info = self._timers.pop(timer_id, None)

        if not info:
            return

        label = info.get("label", "计时器")
        secs = info.get("seconds", 0)
        logger.info("[TimerManager] 到期 id=%s label=%s seconds=%d", timer_id, label, secs)
        log_timer("", "expire", timer_id=timer_id, label=label, seconds=secs)
        log_tool_timer("expire", timer_id=timer_id, label=label, seconds=secs,
                       session_id=info.get("session_id", ""))

        for cb in list(self._expire_callbacks):
            try:
                cb(info)
            except Exception as cb_err:
                logger.warning("[TimerManager] expire callback error: %s", cb_err)
                log_error("timer/expire_callback", str(cb_err), {"timer_id": timer_id})


# ── 全局单例 ──────────────────────────────────────────────────────────────────

_timer_manager = TimerManager()


def start_timer(seconds: int, label: str = "计时器", session_id: str = "") -> str:
    return _timer_manager.start_timer(seconds, label, session_id)


def stop_timer(timer_id: str) -> Optional[Dict]:
    return _timer_manager.stop_timer(timer_id)


def cancel_timer(timer_id: str) -> Optional[Dict]:
    return _timer_manager.stop_timer(timer_id)


def get_timer_status(timer_id: str) -> Optional[Dict]:
    return _timer_manager.get_status(timer_id)


def list_timers() -> List[Dict]:
    return _timer_manager.list_timers()


# ── 意图检测（供 nl_triggers.py 使用）────────────────────────────────────────

import re as _re


def detect_timer_start_intent(msg: str) -> bool:
    if not _re.search(r"(计时|计时器)", msg):
        return False
    if _re.search(r"(开始|启动|请|设置|来).*?(计时|计时器)", msg):
        return True
    if _re.search(r"(计时|计时器).*(开始|启动|吧|一下|请)", msg):
        return True
    seconds, _ = extract_timer_label_and_duration(msg)
    if seconds is not None:
        return True
    if _re.search(r"计时\d+|计时.*?(分钟|秒|小时|分|秒)", msg):
        return True
    return False


def detect_timer_stop_intent(msg: str) -> bool:
    if not _re.search(r"(计时|计时器)", msg):
        return False
    if not _re.search(r"(停止|取消|终止|别|不)", msg):
        return False
    return True


def detect_timer_query_intent(msg: str) -> bool:
    if not _re.search(r"(计时|计时器)", msg):
        return False
    if _re.search(r"(还有|还剩|还在|剩余|多久|多少|状态|进度|在吗|在不在|怎么样|怎样|如何|信息)", msg):
        return True
    if _re.search(r"(计时|计时器)[？?]", msg):
        return True
    return False


def extract_timer_label_and_duration(msg: str):
    """提取计时器标签和时长，返回 (seconds, label) 或 (None, None)。"""
    m = _re.search(r"(\d+)\s*(小时|时|h|分钟|分|m|秒|s)", msg)
    if not m:
        m = _re.search(r"(\d+).{0,8}(小时|时|分钟|分|秒)", msg)
    if not m:
        return None, None

    num = int(m.group(1))
    unit = m.group(2)
    seconds = (num * 3600 if "小时" in unit or unit == "h"
               else num * 60 if "分" in unit or unit == "m"
               else num)

    label = "计时器"
    blacklist = {"请", "我", "帮", "想", "让", "开", "启", "设", "给", "帮我", "开始", "要", "年"}

    all_matches = list(_re.finditer(r"[，。！？、；：\s]([\u4e00-\u9fa5a-zA-Z]{1,3})计时\d+", msg))
    if all_matches:
        candidate = all_matches[-1].group(1)
        if candidate not in blacklist:
            return seconds, candidate

    m_direct = _re.search(r"^[\u4e00-\u9fa5a-zA-Z]{1,3}计时\d+", msg)
    if m_direct:
        raw = m_direct.group(0)
        nums = _re.search(r"\d+", raw)
        candidate = raw[:-(len(nums.group(0)) + 2)] if nums else ""
        if candidate and candidate not in blacklist:
            return seconds, candidate

    m_label = _re.search(r"([\u4e00-\u9fa5a-zA-Z]{1,3})(?:计时器|计时一下|计时吧)", msg)
    if m_label:
        candidate = m_label.group(1)
        if candidate not in blacklist:
            return seconds, candidate

    return seconds, label


def extract_timer_label_only(msg: str) -> Optional[str]:
    """仅提取计时器标签（用于停止/查询）。"""
    blacklist = {"请", "我", "帮", "想", "让", "开", "启", "设", "给", "帮我", "开始", "要", "年",
                 "对", "那", "在", "停止", "取消", "终止", "查询", "还有", "剩余", "还剩"}
    noise = {"时间", "时长", "剩余", "多少", "了吗", "多久", "多长", "间", "长", "久"}

    all_m = list(_re.finditer(r"[，。！？、；：\s]([\u4e00-\u9fa5a-zA-Z]{1,3})(?:计时|计时器|计时一下)?$", msg))
    if all_m:
        candidate = all_m[-1].group(1)
        if candidate not in blacklist and candidate not in noise:
            return candidate

    all_m2 = list(_re.finditer(r"[，。！？、；：\s]([\u4e00-\u9fa5a-zA-Z]{1,3})(?:计时|计时器|计时一下)", msg))
    if all_m2:
        candidate = all_m2[-1].group(1)
        if candidate not in blacklist:
            return candidate

    m1 = _re.search(r"([\u4e00-\u9fa5a-zA-Z]{1,3})(?:计时|计时器|计时一下)(?![量中费用时间])", msg)
    if m1:
        candidate = m1.group(1)
        if candidate not in blacklist:
            return candidate

    m2 = _re.search(r"(?:停止|取消|终止|查询)([,，。！？、；：\s]+)([\u4e00-\u9fa5a-zA-Z]{1,3})(?:计时|计时器)", msg)
    if m2:
        candidate = m2.group(2)
        if candidate not in noise and candidate not in blacklist:
            return candidate

    m3 = _re.search(r"(?:停止|取消|终止|查询)([\u4e00-\u9fa5a-zA-Z]{1,3})计时(?:器)?", msg)
    if m3:
        candidate = m3.group(1)
        if candidate not in noise and candidate not in blacklist:
            return candidate

    return None
