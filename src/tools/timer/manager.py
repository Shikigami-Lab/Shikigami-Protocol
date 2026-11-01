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
