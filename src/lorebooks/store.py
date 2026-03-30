"""CRUD for lorebooks/*.json — shared by API and prompt segment."""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

LOREBOOKS_DIR = "lorebooks"
_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,80}$")


def ensure_lorebooks_dir() -> None:
    os.makedirs(LOREBOOKS_DIR, exist_ok=True)


def validate_lorebook_id(raw: str) -> str:
    lid = (raw or "").strip()
    if not _ID_RE.match(lid):
        raise ValueError("lorebook_id must be 1–80 chars: letters, digits, underscore, hyphen")
    return lid


def lorebook_path(lorebook_id: str) -> str:
    return os.path.join(LOREBOOKS_DIR, f"{lorebook_id}.json")


def load_lorebook_document(lorebook_id: str) -> Optional[Dict[str, Any]]:
    """Return parsed JSON or None if missing/invalid."""
    try:
        lid = validate_lorebook_id(lorebook_id)
    except ValueError:
        return None
    path = lorebook_path(lid)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("[lorebooks] failed to read %s: %s", path, e)
        return None


def list_lorebook_meta() -> List[Dict[str, Any]]:
    """List {lorebook_id, display_name, entry_count} for each file."""
    ensure_lorebooks_dir()
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(LOREBOOKS_DIR):
        return out
    for fname in sorted(os.listdir(LOREBOOKS_DIR)):
        if not fname.endswith(".json"):
            continue
        lid = fname[:-5]
        doc = load_lorebook_document(lid)
        if not doc:
            continue
        entries = doc.get("entries")
        if not isinstance(entries, list):
            entries = doc.get("lorebook") or []
        n = len(entries) if isinstance(entries, list) else 0
        out.append({
            "lorebook_id": doc.get("lorebook_id") or lid,
            "display_name": (doc.get("display_name") or lid).strip() or lid,
            "entry_count": n,
        })
    return out


def save_lorebook_document(lorebook_id: str, doc: Dict[str, Any]) -> None:
    ensure_lorebooks_dir()
    lid = validate_lorebook_id(lorebook_id)
    doc = dict(doc)
    doc["lorebook_id"] = lid
    path = lorebook_path(lid)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def delete_lorebook_file(lorebook_id: str) -> bool:
    try:
        lid = validate_lorebook_id(lorebook_id)
    except ValueError:
        return False
    path = lorebook_path(lid)
    if not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def new_lorebook_id(prefix: str = "lb") -> str:
    """Generate a unique id like lb_a1b2c3d4."""
    ensure_lorebooks_dir()
    for _ in range(20):
        cand = f"{prefix}_{uuid.uuid4().hex[:8]}"
        if not os.path.isfile(lorebook_path(cand)):
            return cand
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def entries_from_document(doc: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not doc:
        return []
    entries = doc.get("entries")
    if isinstance(entries, list):
        return entries
    legacy = doc.get("lorebook")
    return legacy if isinstance(legacy, list) else []


def settings_from_document(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not doc:
        return {}
    st = doc.get("lorebook_settings")
    return st if isinstance(st, dict) else {}


def resolve_lorebook_for_profile(profile: Dict[str, Any]) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    If lorebook_ref points to a valid file, use it.
    Else if ref set but file missing, fall back to inline profile lorebook.
    If no ref, use inline lorebook + profile lorebook_settings (legacy).
    """
    inline_entries = profile.get("lorebook") if isinstance(profile.get("lorebook"), list) else []
    inline_settings = profile.get("lorebook_settings") if isinstance(profile.get("lorebook_settings"), dict) else {}

    ref = (profile.get("lorebook_ref") or "").strip()
    if not ref:
        return list(inline_entries), dict(inline_settings)

    doc = load_lorebook_document(ref)
    if doc:
        return list(entries_from_document(doc)), dict(settings_from_document(doc))

    logger.warning(
        "[lorebooks] profile lorebook_ref=%r missing or invalid file, using inline lorebook",
        ref,
    )
    return list(inline_entries), dict(inline_settings)
