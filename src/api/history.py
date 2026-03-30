"""
history.py — Conversation history endpoint

GET /sessions/{session_id}/history?limit=200
    Returns the last `limit` messages for a session so clients can render
    existing history on load or after a session switch.

    limit=0  → return all messages (caution: may be very large)
    limit>0  → return only the last `limit` messages (default: 200)

Response:
    {
        "session_id": "abc123",
        "total": 11582,
        "messages": [
            {"role": "user",      "content": "...", "timestamp": 1234567890.0, "sender": "用户"},
            {"role": "assistant", "content": "...", "timestamp": 1234567891.5, "sender": "电"},
            ...
        ]
    }
    sender: 发言者标识，单聊为「用户」/人格名，群聊区分多参与方。
"""

import logging
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/sessions/{session_id}/history")
async def get_session_history(
    session_id: str,
    request: Request,
    limit: int = Query(200, ge=0, description="Max messages to return (0 = all)"),
):
    sm = request.app.state.session_manager
    session = sm.get_by_id(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    store = session.conversation_store
    all_msgs = store.get_all()
    total = len(all_msgs)
    subset = all_msgs[-limit:] if limit > 0 else all_msgs
    messages = [asdict(m) for m in subset]
    return {"session_id": session_id, "total": total, "messages": messages}


@router.get("/sessions/{session_id}/chat_history")
async def search_session_history(
    session_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200, description="Messages per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    q: str = Query("", description="Search keyword (empty = all)"),
):
    """Paginated + searchable chat history for the Settings UI.

    Returns newest messages first, with total count and context_since_ts
    so the frontend can show when the short-term context was last reset.
    """
    sm = request.app.state.session_manager
    session = sm.get_by_id(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    store = session.conversation_store
    total = store.count_messages(q)
    messages = store.search_messages(q=q, limit=limit, offset=offset)
    from src.config.effective_config import get_effective_max_history_turns
    max_turns = get_effective_max_history_turns(request.app, session.profile_id)
    return {
        "session_id": session_id,
        "total": total,
        "messages": messages,
        "has_more": offset + len(messages) < total,
        "context_since_ts": store.context_since_ts,
        "context_start_ts": store.get_context_start_ts(max_turns),
    }
