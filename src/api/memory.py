"""记忆 CRUD API — /memory/{profile_id}/...

端点：
  GET    /memory/{profile_id}/status
  GET    /memory/{profile_id}/facts
  POST   /memory/{profile_id}/facts
  PUT    /memory/{profile_id}/facts/{id}
  DELETE /memory/{profile_id}/facts/{id}
  GET    /memory/{profile_id}/vectors       ?query=
  POST   /memory/{profile_id}/vectors/sync_facts  — 批量同步 facts → 向量库
  GET    /memory/{profile_id}/summaries     ?days=3
  DELETE /memory/{profile_id}/summaries/{date}
  POST   /memory/{profile_id}/summaries/generate  — 写日记：按日期范围生成摘要
"""
import asyncio
import hashlib
import logging
import time
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.memory.curation import CurationConflict, curation_for, curation_preset, request_fixed_review, review_model_key
from src.memory.recall import RecallCandidates, experience_links_by_day
from src.memory.extraction_batches import memory_source_excerpt
from src.memory.long_term_store import fact_action_revision
from src.utils.debug_logger import (
    log_app_event,
    log_fact_add,
    log_fact_delete,
    log_fact_clear_all,
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
    pool = RecallCandidates(mgr.facts)
    counts = dict(facts=0, experiences=0, archived=0, expired=0, review=0)
    for fact in pool.facts:
        if fact.id in pool.indexable:
            counts["experiences" if fact.kind == "experience" else "facts"] += 1
        elif not fact.archived and not fact.is_active(pool.now):
            counts["expired"] += 1
        else:
            counts["archived"] += 1
        if fact.review_note:
            counts["review"] += 1
    return {**mgr.status(), "curation": mgr.facts.curation_status(), "counts": counts}


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
    expected_revision: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def _profile_action_revision(profile_id: str, revision: str) -> str:
    return hashlib.sha256(f"{profile_id}\0{revision}".encode("utf-8")).hexdigest()


@router.get("/{profile_id}/facts")
async def get_facts(profile_id: str, request: Request, view: Optional[Literal["facts", "archive"]] = None):
    mgr = _require_manager(request, profile_id)
    pool = RecallCandidates(mgr.facts)
    return {"facts": [
        {**fact.to_dict(), "archive_recallable": fact.id in pool.cold or fact.id in pool.aliases,
         "recallable": fact.id in pool.indexable,
         "action_revision": _profile_action_revision(mgr._profile_id, fact_action_revision(fact))}
        for fact in pool.facts
        if view is None or (fact.id in pool.indexable) == (view == "facts")
    ]}


@router.get("/{profile_id}/facts/{fact_id}/evidence")
async def get_fact_evidence(profile_id: str, fact_id: str, request: Request):
    manager = _require_manager(request, profile_id)
    if manager._profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Memory profile not found")
    fact = manager.facts.get_by_id(fact_id)
    if fact is None:
        raise HTTPException(status_code=404, detail="Fact not found")

    session = request.app.state.session_manager.get_by_id(profile_id)
    groups = getattr(request.app.state, "group_manager", None)
    allowed_groups = set(groups.get_group_ids_for_profile(profile_id)) if groups else set()
    stores = {"single": session.conversation_store} if session and session.profile_id == profile_id else {}
    items = []
    for ref in fact.evidence:
        if not isinstance(ref, dict):
            continue
        source, id = ref.get("source"), ref.get("id")
        if not isinstance(source, str) or type(id) is not int:
            continue
        item = {key: ref[key] for key in ("source", "id", "timestamp", "role", "start_char", "end_char") if key in ref}
        item["available"] = False
        items.append(item)
        if source.startswith("group:") and source[6:] in allowed_groups and source not in stores:
            stores[source] = groups.get_conversation_store(source[6:])
        store = stores.get(source)
        row = store.get_memory_source_message(id) if store else None
        excerpt = memory_source_excerpt(row, ref)
        if excerpt is None:
            continue
        content, start, end = excerpt
        item.update(available=True, role=row["role"], content=content[start:min(end, start + 16000)],
                    truncated=end - start > 16000)
    return {"profile_id": profile_id, "fact_id": fact_id, "items": items}


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
    await mgr.reembed_fact(fact.id)
    return fact.to_dict()


@router.put("/{profile_id}/facts/{fact_id}")
async def update_fact(profile_id: str, fact_id: str, body: FactUpdate, request: Request):
    mgr = _require_manager(request, profile_id)
    kwargs = body.model_dump(exclude={"expected_revision"}, exclude_none=True)
    revision = None
    if body.expected_revision is not None:
        if mgr._profile_id != profile_id:
            raise HTTPException(status_code=404, detail="Memory profile not found")
        revision = _require_current_fact_revision(mgr, fact_id, body.expected_revision)
        kwargs["expected_revision"] = revision
    before = mgr.facts.get_by_id(fact_id)
    if not mgr.facts.update(fact_id, **kwargs):
        if revision is not None:
            raise HTTPException(status_code=409, detail="The memory changed or cannot be edited")
        raise HTTPException(status_code=404, detail="Fact not found")
    after = mgr.facts.get_by_id(fact_id)
    if before and after and (before.content, before.tags) != (after.content, after.tags):
        await mgr.reembed_fact(fact_id)
    return {"ok": True}


@router.delete("/{profile_id}/facts/all")
async def clear_all_facts(profile_id: str, request: Request):
    """清空该人格的全部长期事实（不可撤销）。"""
    mgr = _require_manager(request, profile_id)
    deleted = mgr.facts.clear_all()
    await mgr.prune_curation_index()
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
    if not mgr.facts.delete(fact_id):
        raise HTTPException(status_code=404, detail="Fact not found")
    await mgr.reembed_fact(fact_id)
    return {"ok": True}


# ── Vectors ───────────────────────────────────────────────────────────────────

class BulkCurationPreview(BaseModel):
    cursor: Optional[str] = Field(default=None, max_length=128)
    fact_id: Optional[str] = Field(default=None, min_length=1, max_length=512)


class BulkCurationPromote(BaseModel):
    page_token: str = Field(min_length=1, max_length=128)
    fact_id: str = Field(min_length=1, max_length=512)


class CurationApply(BaseModel):
    preview_id: str = Field(min_length=32, max_length=32)
    fact_ids: Optional[List[str]] = Field(default=None, max_length=24)


class ArchiveRestore(BaseModel):
    archive_revision: str = Field(max_length=32)


class FactReviewDecision(BaseModel):
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["keep", "archive"]
    archive_reason: Optional[Literal["low_value", "unsupported", "cold_memory"]] = None


class FactVersionRestore(BaseModel):
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    version_index: int = Field(ge=0, strict=True)


def _require_curation(request: Request, profile_id: str):
    from src.api.agent import _same_origin
    _same_origin(request)
    manager = _require_manager(request, profile_id)
    if manager._profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Memory profile not found")
    return manager, curation_for(manager)


@router.post("/{profile_id}/curation/preview")
async def preview_curation(profile_id: str, request: Request):
    manager, curation = _require_curation(request, profile_id)
    try:
        preset = curation_preset(manager, request.app)

        async def decide(rows):
            return await request_fixed_review(rows, preset)

        return await curation.preview_fixed(decide, model_key=review_model_key(preset))
    except CurationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception:
        logger.warning("Memory curation preview failed for profile %s", profile_id)
        raise HTTPException(status_code=502, detail="The memory model did not return a valid preview")


@router.post("/{profile_id}/curation/bulk-preview")
async def preview_curation_page(profile_id: str, body: BulkCurationPreview, request: Request):
    manager, curation = _require_curation(request, profile_id)
    try:
        try:
            preset = curation_preset(manager, request.app)
        except CurationConflict:
            if body.fact_id is not None:
                raise
            preset = None

        async def decide(rows):
            return await request_fixed_review(rows, preset)

        return await curation.preview_bulk_fixed(
            decide, cursor=body.cursor, fact_id=body.fact_id, model_key=review_model_key(preset),
        )
    except CurationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception:
        logger.warning("Bulk memory curation preview failed for profile %s", profile_id)
        raise HTTPException(status_code=502, detail="The memory model did not return a valid preview")


@router.post("/{profile_id}/curation/bulk-preview/promote")
async def promote_curation_page_item(profile_id: str, body: BulkCurationPromote, request: Request):
    _, curation = _require_curation(request, profile_id)
    try:
        return curation.promote_bulk(body.page_token, body.fact_id)
    except CurationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _require_current_fact_revision(manager, fact_id: str, expected_revision: str) -> str:
    fact = manager.facts.get_by_id(fact_id)
    if fact is not None:
        revision = fact_action_revision(fact)
        if _profile_action_revision(manager._profile_id, revision) == expected_revision:
            return revision
    raise HTTPException(status_code=409, detail="The memory changed or is unavailable")


@router.post("/{profile_id}/facts/{fact_id}/review")
async def resolve_fact_review(profile_id: str, fact_id: str, body: FactReviewDecision, request: Request):
    manager, _ = _require_curation(request, profile_id)
    if (body.decision == "archive") != (body.archive_reason is not None):
        raise HTTPException(status_code=422, detail="Choose a reason only when archiving")
    revision = _require_current_fact_revision(manager, fact_id, body.expected_revision)
    if not manager.facts.resolve_review(fact_id, revision, body.decision, body.archive_reason):
        raise HTTPException(status_code=409, detail="The memory changed or cannot be reviewed")
    if body.decision == "archive":
        await manager.prune_curation_index()
    return {"ok": True}


@router.post("/{profile_id}/facts/{fact_id}/versions/restore")
async def restore_fact_version(profile_id: str, fact_id: str, body: FactVersionRestore, request: Request):
    manager, _ = _require_curation(request, profile_id)
    revision = _require_current_fact_revision(manager, fact_id, body.expected_revision)
    if not manager.facts.restore_version(fact_id, body.version_index, revision):
        raise HTTPException(status_code=409, detail="The memory changed or this version cannot be restored")
    if manager.vectors and manager.vectors.is_available():
        await manager.reembed_fact(fact_id)
    return {"ok": True}


@router.post("/{profile_id}/curation/apply")
async def apply_curation(profile_id: str, body: CurationApply, request: Request):
    manager, curation = _require_curation(request, profile_id)
    try:
        async with manager._fact_index_lock:
            updates = curation.pending_updates(body.preview_id, body.fact_ids)
            if updates and (manager.vectors or manager._cfg.get("vector_enabled")):
                from src.memory.memory_manager import _vector_document_for_fact, _vector_metadata_for_fact

                vectors = manager.vectors
                if not vectors or not vectors.is_available():
                    raise CurationConflict("Replacement indexing is unavailable. The original remains active.")
                staged_ids = []
                try:
                    for fact in updates:
                        staged = await asyncio.to_thread(vectors.stage_fact, _vector_document_for_fact(fact),
                                                         _vector_metadata_for_fact(fact))
                        if not staged:
                            raise CurationConflict("Replacement indexing failed. The original remains active.")
                        staged_ids.append(staged)
                    receipt = curation.apply(body.preview_id, body.fact_ids)
                except BaseException:
                    for staged in staged_ids:
                        await asyncio.to_thread(vectors.delete, staged)
                    raise
            else:
                receipt = curation.apply(body.preview_id, body.fact_ids)
    except CurationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await manager.prune_curation_index()
    return receipt


@router.post("/{profile_id}/facts/{fact_id}/restore")
async def restore_archived_fact(profile_id: str, fact_id: str, body: ArchiveRestore, request: Request):
    manager, curation = _require_curation(request, profile_id)
    try:
        curation.restore(fact_id, body.archive_revision)
    except CurationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if manager.vectors and manager.vectors.is_available():
        await manager.reembed_fact(fact_id)
    return {"ok": True}



@router.get("/{profile_id}/vectors")
async def get_vectors(profile_id: str, request: Request, query: Optional[str] = None):
    mgr = _require_manager(request, profile_id)
    if not mgr.vectors or not mgr.vectors.is_available():
        return {"available": False, "items": []}
    if query:
        items = await asyncio.to_thread(mgr.vectors.search, query, limit=10)
    else:
        items = await asyncio.to_thread(mgr.vectors.get_all)
    items = mgr.current_vector_rows(items)
    return {"available": True, "vectors": items, "items": items}


@router.post("/{profile_id}/vectors/sync_facts")
async def sync_facts_to_vectors(profile_id: str, request: Request):
    """Repair the index and report coverage of recallable facts."""
    mgr = _require_manager(request, profile_id)
    if not mgr.vectors or not mgr.vectors.is_available():
        raise HTTPException(status_code=503, detail="向量库不可用，请先开启 vector_enabled 并配置 embedding")
    count = await mgr.sync_facts_to_vectors()
    items = await asyncio.to_thread(mgr.vectors.get_all)
    pool = RecallCandidates(mgr.facts)
    indexed = len(mgr.current_vector_rows(items, pool))
    return {"synced": count, "total": len(pool.indexable), "vector_count": indexed}


# ── Summaries ─────────────────────────────────────────────────────────────────

@router.get("/{profile_id}/summaries")
async def get_summaries(profile_id: str, request: Request, days: int = 3):
    mgr = _require_manager(request, profile_id)
    links = experience_links_by_day(mgr.facts.get_all())
    return {"summaries": [
        {**summary, "experience_ids": sorted(links.get(summary.get("date", ""), ()))}
        for summary in mgr.day_store.get_recent(n=days)
    ]}


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
    """Preview expired facts and stale index rows without writing."""
    from datetime import date, timedelta

    from src.core.daily_memory_job import preview_fact_index_maintenance

    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    data = preview_fact_index_maintenance(profile_id, request.app, yesterday)
    return data


@router.post("/{profile_id}/forgetting/run")
async def run_forgetting_now(profile_id: str, request: Request):
    """手动整理已加载人格的事实索引。"""
    from datetime import date, timedelta

    from src.core.daily_memory_job import run_fact_index_maintenance

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
    result = await run_fact_index_maintenance(profile_id, request.app, yesterday, force=True)
    log_app_event("forgetting_run_done", profile_id=profile_id)
    return {"ok": True, "skipped": result is None, "maintenance": result}


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
