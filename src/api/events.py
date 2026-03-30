"""
events.py — Real-time message broadcast via SSE

GET /events/messages?session_id=<id>
    Long-lived SSE connection. The server pushes events whenever any client
    completes a chat turn in this session, so all other open windows/devices
    stay in sync without polling.

Event types sent by server:
    {"type": "connected"}                        — handshake on connect
    {"type": "new_message",                      — a chat turn completed
     "role": "user"|"assistant",
     "content": "...",
     "timestamp": 1234567890.0,
     "client_id": "<sender-uuid>"}               — sender's id (clients filter their own)

Keepalive: a SSE comment (": keepalive") is sent every 25 s so proxies/NAT
don't drop the connection.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from src.core.broadcast import broadcast

logger = logging.getLogger(__name__)


def _log_debug(event: str, **kwargs) -> None:
    try:
        from src.utils.debug_logger import log_debug
        log_debug(event, **kwargs)
    except Exception:
        pass
router = APIRouter()

_KEEPALIVE_INTERVAL = 25  # seconds


@router.get("/events/messages")
async def message_events(session_id: str, request: Request):
    """SSE stream — subscribe to real-time messages for a session."""
    q = broadcast.subscribe(session_id)
    n = broadcast.subscriber_count(session_id)
    _log_debug("sse_connect", session_id=session_id, subscriber_count=n)

    async def generate():
        # Handshake
        yield f"data: {json.dumps({'type': 'connected', 'session_id': session_id})}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_INTERVAL)
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # Keepalive comment — not parsed as a data event by EventSource
                    yield ": keepalive\n\n"
        finally:
            broadcast.unsubscribe(session_id, q)
            n_after = broadcast.subscriber_count(session_id)
            _log_debug("sse_disconnect", session_id=session_id, subscriber_count_after=n_after)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
