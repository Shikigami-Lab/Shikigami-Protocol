"""Find previous task records without copying chat or workspace file contents."""
import json
import re
import sqlite3
import unicodedata
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.agent.business_tools import SearchQuery, _history_db
from src.agent.projection import safe_text

SCAN_LIMIT = 1000
TaskSearchLimit = Annotated[int, Field(strict=True, ge=1, le=10)]


class TaskSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: SearchQuery
    limit: TaskSearchLimit = 5

    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


def _terms(value):
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold())


def _original_requests(session, records, terms):
    ids = list({row["user_message_id"] for row in records if row["user_message_id"] is not None})
    if not ids:
        return {}
    requests = {}
    try:
        with _history_db(session) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            if not {"agent_run_id", "generation_status"}.issubset(columns):
                return {}
            for start in range(0, len(ids), 500):
                page = ids[start:start + 500]
                placeholders = ",".join("?" for _ in page)
                for row in db.execute(
                    f"""SELECT id, agent_run_id, content
                        FROM messages WHERE role='user' AND generation_status='completed'
                        AND id IN ({placeholders})""", page):
                    content = safe_text(row["content"], len(row["content"]))
                    text = " ".join(_terms(content))
                    found = {term for term in terms if term in text}
                    # Use the original snippet around a matching word when possible.
                    original = content.casefold()
                    offsets = [original.find(term) for term in found if term in original]
                    start = max(0, min(offsets, default=0) - 70)
                    excerpt = ("…" if start else "") + content[start:start + 320]
                    requests[row["id"]] = (row["agent_run_id"], excerpt, found)
    except (sqlite3.Error, OSError):
        return {}
    return requests


def search_tasks(store, session, args, *, include_artifacts):
    """Return bounded recent matches scoped to the current single-chat revision."""
    if not session.profile_id or session.id != session.profile_id:
        raise ValueError("Task search requires a host-bound profile session")
    query_terms = set(_terms(args.query))
    if not query_terms:
        return {"ok": True, "matches": [], "has_more_matches": False, "scan_limited": False}
    records = store.task_search_records(session.profile_id, SCAN_LIMIT + 1)
    scanned = records[:SCAN_LIMIT]
    requests = _original_requests(session, scanned, query_terms)
    matches = []
    for row in scanned:
        original = requests.get(row["user_message_id"])
        if row["user_message_id"] is not None and (original is None or original[0] != row["run_id"]):
            continue
        excerpt, found = original[1:] if original is not None else ("", set())
        saved_outcome = row.pop("outcome")
        saved_event = row.pop("terminal_event")
        summary, artifacts = "", []
        if include_artifacts:
            # A previous autonomous summary can mention paths even without an
            # artifact projection, so it shares the same workspace permission.
            outcome = json.loads(saved_outcome or "{}").get("result", {}).get("outcome") or {}
            summary = safe_text(outcome.get("summary", ""))
            event = json.loads(saved_event or "{}")
            artifacts = (event.get("result") or {}).get("artifacts", [])
        text = " ".join(_terms(" ".join([summary, *(file["path"] for file in artifacts)])))
        if not all(term in found or term in text for term in query_terms):
            continue
        matches.append({
            **row, "request_excerpt": excerpt, "summary": summary[:320], "artifacts": artifacts,
        })
        if len(matches) > args.limit:
            break
    return {"ok": True, "matches": matches[:args.limit],
            "has_more_matches": len(matches) > args.limit, "scan_limited": len(records) > SCAN_LIMIT}
