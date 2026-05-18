"""记忆 CRUD API — /memory/{profile_id}/...

端点：
  GET    /memory/{profile_id}/status
  GET    /memory/{profile_id}/facts
  POST   /memory/{profile_id}/facts
  PUT    /memory/{profile_id}/facts/{id}
  DELETE /memory/{profile_id}/facts/{id}
  GET    /memory/{profile_id}/vectors       ?query=
  POST   /memory/{profile_id}/vectors
  POST   /memory/{profile_id}/vectors/sync_facts  — 批量同步 facts → 向量库
  DELETE /memory/{profile_id}/vectors/{memory_id}
  GET    /memory/{profile_id}/summaries     ?days=3
  DELETE /memory/{profile_id}/summaries/{date}
  POST   /memory/{profile_id}/summaries/generate  — 写日记：按日期范围生成摘要
"""
import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.utils.debug_logger import (
    log_app_event,
    log_fact_add,
    log_fact_delete,
    log_fact_clear_all,
    log_vector_delete,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/memory", tags=["memory"])


# ── 辅助：懒创建 MemoryManager ─────────────────────────────────────────────────

def get_or_create_manager_for_session(app, session):
    """按 session 懒创建并缓存 MemoryManager，供单聊/群聊记忆任务使用。无 request 时可调用。"""
    if session is None:
        return None
    profile_id = getattr(session, "profile_id", None)
    if not profile_id:
        return None
    managers = getattr(app.state, "memory_managers", None)
    if managers is None:
        app.state.memory_managers = {}
        managers = app.state.memory_managers
    if profile_id not in managers:
        from src.config.profile_loader import ProfileLoader
        from src.memory.memory_manager import MemoryManager
        config = getattr(app.state, "config", None)
        if config is None:
            return None
        try:
            profile = ProfileLoader().load(profile_id)
        except Exception:
            profile = {}
        app_mem_cfg = config.get_memory_config()
        profile_mem_cfg = (profile or {}).get("memory_config", {})
        managers[profile_id] = MemoryManager(
            profile_id=profile_id,
            storage_root=session.storage_root,
            profile_memory_cfg=profile_mem_cfg,
            app_memory_cfg=app_mem_cfg,
        )
        logger.info("[memory API] 创建 MemoryManager profile=%s", profile_id)
    return managers[profile_id]


def _get_or_create_manager(request: Request, profile_id: str):
    """懒创建并缓存 MemoryManager 到 app.state.memory_managers。"""
    sm = getattr(request.app.state, "session_manager", None)
    if not sm:
        return None
    session = sm.get_by_id(profile_id)
    if session is None:
        for s in sm.list_sessions():
            if s.profile_id == profile_id:
                session = s
                break
    if session is None:
        return None
    return get_or_create_manager_for_session(request.app, session)


def _require_manager(request: Request, profile_id: str):
    mgr = _get_or_create_manager(request, profile_id)
    if mgr is None:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    return mgr


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/{profile_id}/status")
async def get_memory_status(profile_id: str, request: Request):
    mgr = _require_manager(request, profile_id)
    return mgr.status()


# ── Facts CRUD ────────────────────────────────────────────────────────────────

class FactCreate(BaseModel):
    content: str
    category: str = "other"
    weight: float = 1.0
    tags: List[str] = []


class FactUpdate(BaseModel):
    content: Optional[str] = None
    category: Optional[str] = None
    weight: Optional[float] = None
    tags: Optional[List[str]] = None
    pinned: Optional[bool] = None


@router.get("/{profile_id}/facts")
async def get_facts(profile_id: str, request: Request):
    mgr = _require_manager(request, profile_id)
    facts = mgr.facts.get_all()
    return {"facts": [f.to_dict() for f in facts]}


@router.post("/{profile_id}/facts", status_code=201)
async def create_fact(profile_id: str, body: FactCreate, request: Request):
    mgr = _require_manager(request, profile_id)
    fact = mgr.facts.add(
        content=body.content,
        category=body.category,
        weight=body.weight,
        source="manual",
        tags=body.tags,
    )
    if fact is None:
        raise HTTPException(status_code=409, detail="Duplicate fact skipped")
    log_fact_add(
        profile_id,
        fact.id,
        fact.content,
        fact.category,
        fact.source,
        weight=fact.weight,
        tags=fact.tags,
        pinned=fact.pinned,
        emotional_note=fact.emotional_note,
        updated_at=fact.updated_at,
        is_manual=fact.is_manual,
    )
    return fact.to_dict()


@router.put("/{profile_id}/facts/{fact_id}")
async def update_fact(profile_id: str, fact_id: str, body: FactUpdate, request: Request):
    mgr = _require_manager(request, profile_id)
    kwargs = {k: v for k, v in body.model_dump().items() if v is not None}
    ok = mgr.facts.update(fact_id, **kwargs)
    if not ok:
        raise HTTPException(status_code=404, detail="Fact not found")
    return {"ok": True}


@router.delete("/{profile_id}/facts/all")
async def clear_all_facts(profile_id: str, request: Request):
    """清空该人格的全部长期事实（不可撤销）。"""
    mgr = _require_manager(request, profile_id)
    deleted = mgr.facts.clear_all()
    log_fact_clear_all(profile_id, deleted, source="api")
    logger.info("[memory API] 清空事实库 profile=%s deleted=%d", profile_id, deleted)
    return {"ok": True, "deleted": deleted}


@router.delete("/{profile_id}/facts/{fact_id}")
async def delete_fact(profile_id: str, fact_id: str, request: Request):
    mgr = _require_manager(request, profile_id)
    fact = mgr.facts.get_by_id(fact_id)
    if not fact:
        raise HTTPException(status_code=404, detail="Fact not found")
    log_fact_delete(profile_id, fact_id, fact.content, "api")
    ok = mgr.facts.delete(fact_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Fact not found")
    return {"ok": True}


# ── Vectors ───────────────────────────────────────────────────────────────────

class VectorCreate(BaseModel):
    text: str
    metadata: dict = {}


@router.get("/{profile_id}/vectors")
async def get_vectors(profile_id: str, request: Request, query: Optional[str] = None):
    mgr = _require_manager(request, profile_id)
    if not mgr.vectors or not mgr.vectors.is_available():
        return {"available": False, "items": []}
    if query:
        items = await asyncio.to_thread(mgr.vectors.search, query, limit=10)
    else:
        items = await asyncio.to_thread(mgr.vectors.get_all)
    return {"available": True, "vectors": items, "items": items}


@router.post("/{profile_id}/vectors/sync_facts")
async def sync_facts_to_vectors(profile_id: str, request: Request):
    """将 facts store 中所有事实增量同步到向量库。

    已有对应向量（同 fact_id）的跳过，不重复写入。
    返回：{synced, total, vector_count}
    """
    mgr = _require_manager(request, profile_id)
    if not mgr.vectors or not mgr.vectors.is_available():
        raise HTTPException(status_code=503, detail="向量库不可用，请先开启 vector_enabled 并配置 embedding")
    count = await mgr.sync_facts_to_vectors()
    return {
        "synced": count,
        "total": mgr.facts.count(),
        "vector_count": await asyncio.to_thread(mgr.vectors.count),
    }


@router.post("/{profile_id}/vectors", status_code=201)
async def create_vector(profile_id: str, body: VectorCreate, request: Request):
    mgr = _require_manager(request, profile_id)
    if not mgr.vectors or not mgr.vectors.is_available():
        raise HTTPException(status_code=503, detail="Vector store not available")
    mem_id = await asyncio.to_thread(mgr.vectors.add, body.text, metadata=body.metadata)
    if mem_id is None:
        raise HTTPException(status_code=500, detail="Failed to add vector")
    return {"id": mem_id}


@router.delete("/{profile_id}/vectors/all")
async def clear_all_vectors(profile_id: str, request: Request):
    """清空该人格向量库中所有条目（不可撤销）。"""
    mgr = _require_manager(request, profile_id)
    if not mgr.vectors or not mgr.vectors.is_available():
        raise HTTPException(status_code=503, detail="向量库不可用")
    deleted = await asyncio.to_thread(mgr.vectors.clear_all)
    if deleted:
        log_vector_delete(profile_id, deleted, "api_clear_all")
    logger.info("[memory API] 清空向量库 profile=%s deleted=%d", profile_id, deleted)
    return {"ok": True, "deleted": deleted}


@router.delete("/{profile_id}/vectors/{memory_id}")
async def delete_vector(profile_id: str, memory_id: str, request: Request):
    mgr = _require_manager(request, profile_id)
    if not mgr.vectors:
        raise HTTPException(status_code=503, detail="Vector store not available")
    ok = await asyncio.to_thread(mgr.vectors.delete, memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Vector memory not found")
    return {"ok": True}


# ── Summaries ─────────────────────────────────────────────────────────────────

@router.get("/{profile_id}/summaries")
async def get_summaries(profile_id: str, request: Request, days: int = 3):
    mgr = _require_manager(request, profile_id)
    return {"summaries": mgr.day_store.get_recent(n=days)}


@router.get("/{profile_id}/summaries/preview")
async def get_summaries_preview(
    profile_id: str,
    request: Request,
    start_date: str,
    days: int = 7,
):
    """写日记预览：返回范围内每天的对话条数及是否已有摘要，不生成。"""
    mgr = _require_manager(request, profile_id)
    sm = getattr(request.app.state, "session_manager", None)
    if sm is None:
        raise HTTPException(status_code=503, detail="Session manager not available")
    session = None
    for s in sm.list_sessions():
        if s.profile_id == profile_id:
            session = s
            break
    if session is None:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    try:
        from src.config.profile_loader import ProfileLoader
        profile = ProfileLoader().load(profile_id)
    except Exception:
        profile = {}
    per_date = mgr.get_summary_range_preview(
        session.conversation_store, start_date, days,
        session=session, app=request.app, profile=profile,
    )
    return {"per_date": per_date}


@router.delete("/{profile_id}/summaries/{date_str}")
async def delete_summary(profile_id: str, date_str: str, request: Request):
    mgr = _require_manager(request, profile_id)
    ok = mgr.day_store.delete(date_str)
    if not ok:
        raise HTTPException(status_code=404, detail="Summary not found")
    return {"ok": True}


@router.delete("/{profile_id}/summaries")
async def clear_all_summaries(profile_id: str, request: Request):
    """清空当前人格的全部对话摘要（不可撤销）。"""
    mgr = _require_manager(request, profile_id)
    n = mgr.day_store.clear_all()
    return {"ok": True, "deleted": n}


class GenerateSummariesBody(BaseModel):
    start_date: str  # YYYY-MM-DD
    days: int = 1
    force: bool = False  # 是否覆盖已有摘要


@router.post("/{profile_id}/summaries/generate")
async def generate_summaries(
    profile_id: str,
    request: Request,
    body: GenerateSummariesBody,
):
    """写日记：对 start_date 起的连续 days 天逐日生成对话摘要。"""
    mgr = _require_manager(request, profile_id)
    sm = getattr(request.app.state, "session_manager", None)
    if sm is None:
        raise HTTPException(status_code=503, detail="Session manager not available")
    session = None
    for s in sm.list_sessions():
        if s.profile_id == profile_id:
            session = s
            break
    if session is None:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    try:
        from src.config.profile_loader import ProfileLoader
        profile = ProfileLoader().load(profile_id)
    except Exception:
        profile = {}
    store = session.conversation_store
    result = await mgr.generate_day_summaries_range(
        body.start_date,
        body.days,
        store,
        profile,
        request.app,
        session=session,
        force=body.force,
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/{profile_id}/forgetting/preview")
async def get_forgetting_preview(profile_id: str, request: Request):
    """预览「执行一次每日记忆/遗忘任务」将执行的内容，不实际执行。"""
    from datetime import date, timedelta

    from src.core.daily_memory_job import preview_forgetting_for_profile

    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    data = preview_forgetting_for_profile(profile_id, request.app, yesterday)
    return data


@router.post("/{profile_id}/forgetting/run")
async def run_forgetting_now(profile_id: str, request: Request):
    """手动执行一次遗忘任务（衰减、强化、孤儿清理），与「写日记」类似。仅对已加载的 session 有效。"""
    from datetime import date, timedelta

    from src.core.daily_memory_job import run_forgetting_for_profile

    log_app_event("forgetting_run_request", profile_id=profile_id)
    sm = getattr(request.app.state, "session_manager", None)
    if not sm:
        raise HTTPException(status_code=503, detail="Session manager not available")
    session = None
    for s in sm.list_sessions():
        if s.profile_id == profile_id:
            session = s
            break
    if not session:
        log_app_event("forgetting_run_skip", profile_id=profile_id, reason="profile_not_loaded")
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found or not loaded")
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    await run_forgetting_for_profile(profile_id, request.app, yesterday, force=True)
    log_app_event("forgetting_run_done", profile_id=profile_id)
    return {"ok": True, "message": "已执行一次遗忘任务（衰减、强化、孤儿清理）"}


# ── 记忆变更日志 + 回滚 ───────────────────────────────────────────────────────

@router.get("/{profile_id}/changelog")
async def get_memory_changelog(profile_id: str, request: Request):
    """返回该人格的记忆变更历史（最近 20 条，最新在前）。无需人格已加载。"""
    import os
    from src.memory.memory_changelog import get_changelog
    from src.utils.paths import get_project_root
    storage_root = os.path.join(get_project_root(), "profiles", profile_id)
    return {"changelog": get_changelog(storage_root)}


class MemoryRollbackBody(BaseModel):
    run_id: str


@router.post("/{profile_id}/changelog/rollback")
async def rollback_memory_changelog(profile_id: str, body: MemoryRollbackBody, request: Request):
    """回滚某次记忆变更：撤销该次权重变化、删除其新增摘要、重新嵌入其移除的向量。
    需人格已加载（回滚要操作 MemoryManager）。"""
    mgr = _require_manager(request, profile_id)
    from src.memory import memory_changelog
    result = await memory_changelog.rollback(mgr._storage_root, body.run_id, mgr)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "rollback_failed"))
    log_app_event("memory_rollback", profile_id=profile_id, run_id=body.run_id,
                  restored=result.get("restored"), skipped=result.get("skipped"))
    return result
