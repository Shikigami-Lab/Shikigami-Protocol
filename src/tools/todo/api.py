"""待办事项 REST API — /sessions/{session_id}/todos

GET    /sessions/{sid}/todos           — 列出所有待办
POST   /sessions/{sid}/todos           — 新增待办
PATCH  /sessions/{sid}/todos/{id}      — 更新（done/content）
DELETE /sessions/{sid}/todos/{id}      — 删除
DELETE /sessions/{sid}/todos           — 清空已完成

数据层统一走 TodoStore（支持 priority / due_date）。
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.tools.todo.store import TodoStore
from src.utils.debug_logger import log_tool_todo

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_session(request: Request, session_id: str):
    sm = request.app.state.session_manager
    session = sm.get_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


# ── 端点 ─────────────────────────────────────────────────────────────────────

@router.get("/sessions/{session_id}/todos")
async def list_todos(session_id: str, request: Request):
    session = _get_session(request, session_id)
    store = TodoStore(session.storage_root)
    return {"todos": [t.to_dict() for t in store.get_all()]}


class TodoCreate(BaseModel):
    content: str
    priority: int = 3
    due_date: Optional[float] = None


@router.post("/sessions/{session_id}/todos", status_code=201)
async def create_todo(session_id: str, body: TodoCreate, request: Request):
    session = _get_session(request, session_id)
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content cannot be empty")
    store = TodoStore(session.storage_root)
    item = store.add(content, due_date=body.due_date, priority=body.priority)
    log_tool_todo("create", session_id=session_id, todo_id=item.id,
                  content=content, priority=body.priority)
    return item.to_dict()


class TodoPatch(BaseModel):
    done: Optional[bool] = None
    content: Optional[str] = None


@router.patch("/sessions/{session_id}/todos/{todo_id}")
async def update_todo(session_id: str, todo_id: str, body: TodoPatch, request: Request):
    session = _get_session(request, session_id)
    store = TodoStore(session.storage_root)
    item = store.update(todo_id, done=body.done, content=body.content)
    if item is None:
        raise HTTPException(status_code=404, detail="Todo not found")
    log_tool_todo("update", session_id=session_id, todo_id=todo_id,
                  content=item.content, done=item.done)
    return {"ok": True, "todo": item.to_dict()}


@router.delete("/sessions/{session_id}/todos/{todo_id}")
async def delete_todo(session_id: str, todo_id: str, request: Request):
    session = _get_session(request, session_id)
    store = TodoStore(session.storage_root)
    item = store.delete_by_id(todo_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Todo not found")
    log_tool_todo("delete", session_id=session_id, todo_id=todo_id,
                  content=item.content)
    return {"ok": True}


@router.delete("/sessions/{session_id}/todos")
async def clear_done_todos(session_id: str, request: Request):
    """清空已完成的待办。"""
    session = _get_session(request, session_id)
    store = TodoStore(session.storage_root)
    removed = store.clear_done()
    log_tool_todo("clear_done", session_id=session_id, removed_count=removed)
    return {"ok": True, "removed": removed}
