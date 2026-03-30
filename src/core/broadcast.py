"""
broadcast.py — Session-scoped SSE broadcast manager

Each session can have multiple connected clients (Electron window, phone browser, etc.).
When a new message pair completes, chat.py calls broadcast.push() to notify all
other clients so they can append the message without polling.

Usage:
    from src.core.broadcast import broadcast

    # Subscribe (returns an asyncio.Queue):
    q = broadcast.subscribe(session_id)

    # Push to everyone except the sender:
    await broadcast.push(session_id, data, exclude=q)

    # Unsubscribe on disconnect:
    broadcast.unsubscribe(session_id, q)
"""

import asyncio
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


def _log_debug(event: str, **data: Any) -> None:
    try:
        from src.utils.debug_logger import log_debug
        log_debug(event, **data)
    except Exception:
        pass


class BroadcastManager:
    def __init__(self):
        # session_id → list of subscriber queues
        self._queues: Dict[str, List[asyncio.Queue]] = {}

    def subscribe(self, session_id: str) -> asyncio.Queue:
        """Register a new SSE client for this session. Returns its queue."""
        q: asyncio.Queue = asyncio.Queue()
        self._queues.setdefault(session_id, []).append(q)
        logger.debug(f"[broadcast] +subscriber session={session_id} total={len(self._queues[session_id])}")
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        """Remove a client queue (called when SSE connection closes)."""
        bucket = self._queues.get(session_id, [])
        try:
            bucket.remove(q)
        except ValueError:
            pass
        logger.debug(f"[broadcast] -subscriber session={session_id} total={len(bucket)}")

    async def push(
        self,
        session_id: str,
        data: Any,
        exclude: Optional[asyncio.Queue] = None,
    ) -> None:
        """Push data to all subscribers of session_id except the excluded queue."""
        for q in list(self._queues.get(session_id, [])):
            if q is exclude:
                continue
            await q.put(data)

    async def push_all(self, data: Any) -> None:
        """Push data to ALL subscribers across all sessions (e.g. timer expiry)."""
        for queues in list(self._queues.values()):
            for q in list(queues):
                await q.put(data)

    async def push_one(self, data: Any) -> None:
        """Push data to exactly ONE subscriber (any).用于 timer 到期等全局事件，避免同一客户端因多连接收到两次。"""
        total = sum(len(queues) for queues in self._queues.values())
        if data.get("type") == "timer_expired":
            _log_debug("broadcast_push_one_timer_expired", total_subscribers=total, per_session={k: len(v) for k, v in self._queues.items()})
        for queues in list(self._queues.values()):
            for q in list(queues):
                await q.put(data)
                return
        return

    async def push_one_for_session(self, session_id: str, data: Any) -> None:
        """仅推给该 session 的任意一个订阅者（用于计时器到期：只让一台设备触发 LLM，回复会广播到该 session 所有端）。"""
        queues = list(self._queues.get(session_id, []))
        if not queues:
            return
        await queues[0].put(data)

    def subscriber_count(self, session_id: str) -> int:
        return len(self._queues.get(session_id, []))


# Module-level singleton — import this everywhere
broadcast = BroadcastManager()
