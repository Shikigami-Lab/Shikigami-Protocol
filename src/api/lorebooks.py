"""World book library: lorebooks/*.json — CRUD + Tavern import (entries only)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from src.api.profiles_import import (
    _build_lorebook,
    _extract_character_name,
    _extract_json_from_png,
    _fallback_name_from_filename,
    _normalize_card,
)
from src.lorebooks.entry_utils import normalize_lorebook_entry_for_storage
from src.lorebooks.store import (
    delete_lorebook_file,
    list_lorebook_meta,
    load_lorebook_document,
    new_lorebook_id,
    save_lorebook_document,
    validate_lorebook_id,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/lorebooks", tags=["lorebooks"])


def _clean_entries(raw: Any, lb_defaults: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        ne = normalize_lorebook_entry_for_storage(item, lb_defaults)
        if ne:
            out.append(ne)
    return out


def _clamp_scan_turns(v: Any) -> int:
    try:
        n = int(v)
        return max(1, min(50, n))
    except (TypeError, ValueError):
        return 10


def _merge_lorebook_settings_put(
    lid: str,
    body_settings: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """合并旧文件中的 lorebook_settings 与本次 PUT，保证 scan_turns / default_entry_mode 完整。"""
    prev = load_lorebook_document(lid) or {}
    prev_st = prev.get("lorebook_settings") if isinstance(prev.get("lorebook_settings"), dict) else {}
    st: Dict[str, Any] = dict(prev_st)
    if isinstance(body_settings, dict):
        if "scan_turns" in body_settings:
            st["scan_turns"] = _clamp_scan_turns(body_settings.get("scan_turns"))
        if "default_entry_mode" in body_settings:
            de = body_settings.get("default_entry_mode")
            if de in ("keyword", "constant"):
                st["default_entry_mode"] = de
    if "scan_turns" not in st:
        st["scan_turns"] = 10
    if st.get("default_entry_mode") not in ("keyword", "constant"):
        st["default_entry_mode"] = "keyword"
    return st


class LorebookPutBody(BaseModel):
    display_name: str = ""
    entries: List[Dict[str, Any]] = Field(default_factory=list)
    lorebook_settings: Dict[str, Any] = Field(default_factory=dict)


class LorebookCreateBody(BaseModel):
    lorebook_id: str = ""
    display_name: str = "新世界书"


@router.get("")
async def list_lorebooks():
    return {"ok": True, "lorebooks": list_lorebook_meta()}


@router.get("/{lorebook_id}")
async def get_lorebook(lorebook_id: str):
    doc = load_lorebook_document(lorebook_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Lorebook not found")
    lid = doc.get("lorebook_id") or lorebook_id
    entries = doc.get("entries")
    if not isinstance(entries, list):
        entries = doc.get("lorebook") or []
    st = doc.get("lorebook_settings") if isinstance(doc.get("lorebook_settings"), dict) else {}
    return {
        "ok": True,
        "lorebook_id": lid,
        "display_name": doc.get("display_name") or lid,
        "entries": entries,
        "lorebook_settings": st,
    }


@router.put("/{lorebook_id}")
async def put_lorebook(lorebook_id: str, body: LorebookPutBody):
    try:
        lid = validate_lorebook_id(lorebook_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    st = _merge_lorebook_settings_put(lid, body.lorebook_settings if isinstance(body.lorebook_settings, dict) else None)
    lb_defaults = {
        "default_entry_mode": st.get("default_entry_mode", "keyword"),
        "scan_turns": st.get("scan_turns", 10),
    }
    entries = _clean_entries(body.entries, lb_defaults)
    doc = {
        "lorebook_id": lid,
        "display_name": (body.display_name or lid).strip() or lid,
        "entries": entries,
        "lorebook_settings": st,
    }
    save_lorebook_document(lid, doc)
    return {"ok": True, "lorebook_id": lid, "entry_count": len(entries)}


@router.post("")
async def create_lorebook(body: LorebookCreateBody):
    raw_id = (body.lorebook_id or "").strip()
    try:
        lid = validate_lorebook_id(raw_id) if raw_id else new_lorebook_id()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if load_lorebook_document(lid):
        raise HTTPException(status_code=409, detail=f"lorebook_id '{lid}' already exists")
    doc = {
        "lorebook_id": lid,
        "display_name": (body.display_name or lid).strip() or lid,
        "entries": [],
        "lorebook_settings": {"scan_turns": 10, "default_entry_mode": "keyword"},
    }
    save_lorebook_document(lid, doc)
    return {"ok": True, "lorebook_id": lid}


@router.delete("/{lorebook_id}")
async def remove_lorebook(lorebook_id: str):
    if not delete_lorebook_file(lorebook_id):
        raise HTTPException(status_code=404, detail="Lorebook not found")
    return {"ok": True}


@router.post("/import/tavern")
async def import_tavern_lorebook_only(file: UploadFile = File(...)):
    """从 Tavern 角色卡或 **单独导出的世界书 JSON** 提取条目，写入 lorebooks/*.json。"""
    filename = file.filename or ""
    data = await file.read()
    if filename.lower().endswith(".png"):
        raw = _extract_json_from_png(data)
    elif filename.lower().endswith(".json"):
        try:
            raw = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise HTTPException(status_code=422, detail=f"JSON decode error: {e}")
    else:
        raise HTTPException(status_code=415, detail="Only .json or .png supported")

    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Invalid card root")

    card = _normalize_card(raw)
    entries = _build_lorebook(card)
    if not entries:
        raise HTTPException(
            status_code=422,
            detail="未找到可用条目：需要角色卡内的 character_book.entries，或 SillyTavern 世界书导出的根级 entries（可为对象或数组）",
        )

    # 文件 ID（lorebooks/<id>.json）：优先用上传文件名（去扩展名），避免 JSON 内超长 name 被截成 Statu 等丑 slug
    name_from_file = _fallback_name_from_filename(filename)
    name_from_card = _extract_character_name(card)
    name_for_slug = (name_from_file.strip() if name_from_file else "") or name_from_card or "imported"
    slug = re.sub(r"[^a-zA-Z0-9_]", "_", name_for_slug)[:28].strip("_") or "card"
    lid = new_lorebook_id(prefix=f"st_{slug}")

    # 展示名仍以卡内书名为优先（无则退回文件名）
    display_title = name_from_card or name_from_file or "imported"
    doc = {
        "lorebook_id": lid,
        "display_name": f"《{display_title}》世界书",
        "entries": entries,
        "lorebook_settings": {"scan_turns": 10},
    }
    save_lorebook_document(lid, doc)
    return {
        "ok": True,
        "lorebook_id": lid,
        "display_name": doc["display_name"],
        "entry_count": len(entries),
        "message": f"已导入世界书「{doc['display_name']}」（{len(entries)} 条）",
    }
