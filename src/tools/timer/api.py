"""计时器 REST API — GET/POST/DELETE /timers

GET  /timers               — 列出所有活跃计时器（含剩余秒数）
POST /timers               — 启动新计时器
DELETE /timers/{timer_id}  — 停止/取消指定计时器
"""
import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.tools.timer.manager import list_timers, start_timer, stop_timer

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/timers")
async def get_timers():
    """返回所有活跃计时器，附带剩余秒数。多端共用同一列表。"""
    now = time.time()
    result = []
    for t in list_timers():
        remaining = max(0.0, t["end_timestamp"] - now)
        result.append({
            "id":             t["id"],
            "label":          t["label"],
            "seconds":        t["seconds"],
            "started_at":     t["started_at"],
            "end_timestamp":  float(t["end_timestamp"]),
            "remaining":      int(remaining),
            "running":        t.get("running", True),
            "session_id":     t.get("session_id", ""),
        })
    return {"timers": result}


class TimerCreateRequest(BaseModel):
    seconds: int
    label: str = "计时器"
    session_id: str = ""  # 当前会话（人格 id 或 group:xxx），到期时只推给该 session 的订阅者


@router.post("/timers")
async def create_timer(body: TimerCreateRequest):
    if body.seconds <= 0:
        raise HTTPException(status_code=400, detail="seconds must be > 0")
    timer_id = start_timer(body.seconds, body.label, body.session_id or "")
    return {"ok": True, "timer_id": timer_id}


@router.delete("/timers/{timer_id}")
async def cancel_timer_endpoint(timer_id: str):
    info = stop_timer(timer_id)
    if info is None:
        raise HTTPException(status_code=404, detail="Timer not found")
    return {"ok": True}
