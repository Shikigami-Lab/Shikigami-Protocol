"""Bound, read-only project tools; runtime owns authorization and task_context.

History reads the original session ConversationStore database in SQLite ro mode.
It never constructs a store (which would initialize/migrate it), enumerates
profiles, or copies history into the task store. The host must pass the session
already bound to this run and check tool enablement/revision before dispatch.
"""
import asyncio
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import inspect
import json
from pathlib import Path
import sqlite3
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator


MAX_MESSAGES = 20
MAX_HISTORY_OFFSET = 10000
MAX_CONTEXT = 10
MAX_CONTENT = 4000
MAX_QUERY = 500
MAX_SEARCH_RESULTS = 5
MAX_METADATA_READ = 65536
_METADATA_FIELDS = ("metadata", "run_id", "status", "generation_status", "agent_run_id")


def _date_text(value: str) -> str:
    if value and date.fromisoformat(value).isoformat() != value:
        raise ValueError("Dates must use YYYY-MM-DD")
    return value


Query = Annotated[str, Field(strict=True, max_length=MAX_QUERY)]
SearchQuery = Annotated[str, Field(strict=True, min_length=1, max_length=MAX_QUERY)]
DateText = Annotated[str, Field(strict=True, pattern=r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2})?$"), AfterValidator(_date_text)]
HistoryLimit = Annotated[int, Field(strict=True, ge=1, le=MAX_MESSAGES)]
HistoryOffset = Annotated[int, Field(strict=True, ge=0, le=MAX_HISTORY_OFFSET)]
HistoryOrder = Literal["newest", "oldest"]
MessageId = Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]
ContextCount = Annotated[int, Field(strict=True, ge=0, le=MAX_CONTEXT)]
RunId = Annotated[str, Field(strict=True, max_length=128)]


def history_date_bounds(start_date: str, end_date: str) -> tuple[float | None, float | None]:
    """Inclusive local dates become [local midnight, next local midnight).

    Convert each boundary separately so a local DST day may be 23 or 25 hours.
    Naive datetime.timestamp uses the host timezone for that date, not today's
    fixed UTC offset. Unsupported platform dates fail validation explicitly.
    """
    start = date.fromisoformat(start_date) if start_date else None
    end = date.fromisoformat(end_date) if end_date else None
    if start and end and start > end:
        raise ValueError("start_date must not be after end_date")
    try:
        return (
            datetime.combine(start, time.min).timestamp() if start else None,
            datetime.combine(end + timedelta(days=1), time.min).timestamp() if end else None,
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("Date range cannot be represented on this host") from exc


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HistorySearchArgs(_Arguments):
    query: Query = ""
    start_date: DateText = ""
    end_date: DateText = ""
    limit: HistoryLimit = 5
    order: HistoryOrder = "newest"
    offset: HistoryOffset = 0

    @model_validator(mode="after")
    def valid_range(self):
        history_date_bounds(self.start_date, self.end_date)
        return self


class HistoryContextArgs(_Arguments):
    message_id: MessageId
    before: ContextCount = 3
    after: ContextCount = 3


class WebSearchArgs(_Arguments):
    query: SearchQuery

    @model_validator(mode="after")
    def nonblank_query(self):
        if not self.query.strip():
            raise ValueError("query must not be blank")
        return self


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


@contextmanager
def _history_db(session):
    # Do not access Session.conversation_store: its lazy accessor writes files.
    store = getattr(session, "_store", None)
    path = Path(store._db_path) if store is not None else Path(session.storage_root) / "chat_records.db"
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")  # Anchor and surrounding messages share a snapshot.
        yield db
    finally:
        db.close()


def _projection(db, content_start="1") -> str:
    columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
    # content_start is a host-authored SQL expression, never model text.
    parts = ["id", "substr(role,1,33) AS role", f"substr(content,{content_start},{MAX_CONTENT + 1}) AS content",
             f"({content_start})-1 AS content_offset", "length(content) AS content_length",
             "timestamp", "substr(sender,1,257) AS sender" if "sender" in columns else "'' AS sender"]
    # Preserve available provenance/incompleteness without requiring a migration.
    for column in ("incomplete", *_METADATA_FIELDS):
        if column in columns:
            if column == "incomplete":
                parts.append(column)
            else:
                cap = MAX_METADATA_READ if column == "metadata" else MAX_CONTENT
                parts.append(f"substr({column},1,{cap + 1}) AS {column}")
    return ", ".join(parts)


def _metadata(result, value):
    """Keep small JSON intact; retain bounded provenance when other data is huge.

    Beyond the bounded read, completeness is explicitly unknown. Do not turn
    clipped JSON into an apparently authoritative string or default to complete.
    """
    if value is None:
        return
    if len(value) > MAX_CONTENT:
        result["metadata_truncated"] = True
        result["metadata_excerpt"] = value[:MAX_CONTENT]
    try:
        if len(value) > MAX_METADATA_READ:
            raise ValueError("Metadata exceeds bounded read")
        parsed = json.loads(value)
        json.dumps(parsed, allow_nan=False)
        if len(value) <= MAX_CONTENT:
            result["metadata"] = parsed
        elif isinstance(parsed, dict):
            result["metadata"] = {
                key: parsed[key] for key in (
                    "incomplete", "generation_status", "agent_run_id", "run_id", "status", "reason",
                ) if key in parsed and (parsed[key] is None or type(parsed[key]) in (bool, int, float)
                                        or isinstance(parsed[key], str) and len(parsed[key]) <= 256)
            }
        else:
            raise ValueError("Oversized non-object metadata")
    except (ValueError, RecursionError):
        result["metadata"] = None
        result["metadata_unavailable"] = True
        result["metadata_excerpt"] = value[:MAX_CONTENT]


def _message(row) -> dict:
    result = dict(row)
    try:
        local_time = datetime.fromtimestamp(result["timestamp"]).astimezone()
        result["local_datetime"] = local_time.isoformat(timespec="seconds")
        result["local_date"] = local_time.date().isoformat()
    except (OverflowError, OSError, TypeError, ValueError):
        result["local_datetime"] = None
        result["local_date"] = None
    content = result.get("content") or ""
    result["content"] = content[:MAX_CONTENT]
    result["content_truncated"] = (len(content) > MAX_CONTENT or result["content_offset"] > 0
                                   or result["content_length"] > len(result["content"]))
    for key, cap in (("role", 32), ("sender", 256)):
        if isinstance(result.get(key), str) and len(result[key]) > cap:
            result[key] = result[key][:cap]
            result[key + "_truncated"] = True
    for key in _METADATA_FIELDS:
        value = result.get(key)
        if key == "metadata" and isinstance(value, str):
            _metadata(result, value)
        elif isinstance(value, str) and len(value) > MAX_CONTENT:
            # Opaque source identities must not become a different shortened ID.
            result[key] = None
            result[key + "_excerpt"] = value[:MAX_CONTENT]
            result[key + "_truncated"] = True
    return result


def read_task_message(session, message_id: int, *, expected_run_id: str | None = None) -> dict | None:
    """Synchronously read one bounded original message for host task linkage.

    Returns None only when that ID is absent. Invalid session/IDs raise ValueError
    (including Pydantic ValidationError); unavailable databases raise sqlite3.Error
    or OSError. Runtime task-context/resume callers must pass expected_run_id from
    their authorized LinkStore record. That guard accepts only the complete,
    untruncated user request marked with that run ID; mismatches raise ValueError.
    The runtime still owns profile/revision/run authorization. This helper is
    deliberately not registered as an MCP tool.
    It does not initialize a store or mutate history.
    """
    if not getattr(session, "profile_id", None) or getattr(session, "id", None) != session.profile_id:
        raise ValueError("Task history requires a host-bound profile session")
    if expected_run_id is not None and (not isinstance(expected_run_id, str)
            or not expected_run_id.strip() or len(expected_run_id) > 256):
        raise ValueError("Invalid expected task run ID")
    args = HistoryContextArgs(message_id=message_id, before=0, after=0)
    with _history_db(session) as db:
        row = db.execute(f"SELECT {_projection(db)} FROM messages WHERE id=?", (args.message_id,)).fetchone()
    result = _message(row) if row is not None else None
    if result is not None and expected_run_id is not None:
        if (result.get("agent_run_id") != expected_run_id or result["role"] != "user"
                or result.get("generation_status") != "completed" or result["content_truncated"]):
            raise ValueError("Original task request is mismatched, incomplete, or truncated")
    return result


def _history_search(session, args: HistorySearchArgs) -> dict:
    start, end = history_date_bounds(args.start_date, args.end_date)
    clauses, values = [], {"excerpt_query": args.query, "limit": args.limit + 1}
    if args.query:
        # Literal substring: model-supplied % and _ are not SQL wildcards.
        query = args.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("content LIKE :query ESCAPE '\\'")
        values["query"] = "%" + query + "%"
    if start is not None:
        clauses.append("timestamp >= :start")
        values["start"] = start
    if end is not None:
        clauses.append("timestamp < :end")
        values["end"] = end
    with _history_db(session) as db:
        # Keep the first literal match in view, with up to 200 preceding chars.
        content_start = "max(1,instr(lower(content),lower(:excerpt_query))-200)" if args.query else "1"
        direction = "ASC" if args.order == "oldest" else "DESC"
        rows = db.execute(
            f"SELECT {_projection(db, content_start)} FROM messages WHERE {' AND '.join(clauses) or '1=1'} "
            f"ORDER BY timestamp {direction}, id {direction} LIMIT :limit OFFSET :offset",
            {**values, "offset": args.offset},
        ).fetchall()
    messages = [_message(row) for row in rows[:args.limit]]
    has_more = len(rows) > args.limit
    next_offset = args.offset + len(messages)
    page_limit = has_more and next_offset > MAX_HISTORY_OFFSET
    return {"ok": True, "source": "conversation_store", "order": args.order, "offset": args.offset,
            "messages": messages, "has_more": has_more,
            "next_offset": next_offset if has_more and not page_limit else None,
            "pagination_limit_reached": page_limit}


def _history_context(session, args: HistoryContextArgs) -> dict:
    with _history_db(session) as db:
        projection = _projection(db)
        anchor = db.execute(f"SELECT {projection} FROM messages WHERE id=?", (args.message_id,)).fetchone()
        if anchor is None:
            return _error("not_found", "Message was not found in this profile's history")
        key = (anchor["timestamp"], anchor["id"])
        before = db.execute(
            f"SELECT {projection} FROM messages WHERE (timestamp,id) < (?,?) "
            "ORDER BY timestamp DESC,id DESC LIMIT ?", (*key, args.before),
        ).fetchall()
        after = db.execute(
            f"SELECT {projection} FROM messages WHERE (timestamp,id) > (?,?) "
            "ORDER BY timestamp,id LIMIT ?", (*key, args.after),
        ).fetchall()
    return {"ok": True, "source": "conversation_store", "message_id": args.message_id,
            "messages": [_message(row) for row in [*reversed(before), anchor, *after]]}


async def _web_search(args: WebSearchArgs) -> dict:
    from src.tools.websearch.searcher import search

    # The legacy searcher silently returns [] on failure. Do not label that as
    # a successful empty search. Host integration adds this backwards-compatible
    # opt-in flag to search and its provider helpers; never duplicate providers.
    if "raise_on_error" not in inspect.signature(search).parameters:
        return _error("search_unavailable", "Search requires an error-reporting provider adapter")
    try:
        rows = await search(args.query, max_results=MAX_SEARCH_RESULTS, raise_on_error=True)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Invalid search response")
        results = []
        for row in rows[:MAX_SEARCH_RESULTS]:
            result = {}
            for key, limit in (("title", 300), ("snippet", 2000), ("url", 2048)):
                value = row.get(key, "")
                if not isinstance(value, str):
                    raise ValueError("Invalid search field")
                result[key] = value[:limit] if key != "url" or len(value) <= limit else ""
                if len(value) > limit:
                    result[key + "_truncated"] = True
                    if key == "url":
                        result["url_excerpt"] = value[:limit]
            result["citation_available"] = bool(result["url"])
            results.append(result)
        return {"ok": True, "source": "web_search", "query": args.query, "results": results,
                "has_more": len(rows) > MAX_SEARCH_RESULTS}
    except Exception:
        # Provider exceptions may contain URLs, API keys, or transport details.
        return _error("search_failed", "Web search failed; no reliable search results are available")


async def call_business_tool(app, session, name: str, args: dict) -> dict:
    """Return JSON dictionaries for history/search; task_context stays in runtime.

    ``app`` is reserved for host integration; no global app/profile lookup is
    performed. All errors are explicit and contain no raw database/provider text.
    """
    models = {"history_search": HistorySearchArgs, "history_context": HistoryContextArgs,
              "web_search": WebSearchArgs}
    if name not in models:
        return _error("unknown_tool", "Unknown business tool")
    try:
        parsed = models[name].model_validate(args)
    except (ValidationError, ValueError, TypeError):
        return _error("invalid_arguments", "Invalid tool arguments, bounds, or date range")
    if not getattr(session, "profile_id", None) or getattr(session, "id", None) != session.profile_id:
        return _error("invalid_session", "Tool requires a host-bound profile session")
    if name == "web_search":
        return await _web_search(parsed)
    try:
        action = _history_search if name == "history_search" else _history_context
        return await asyncio.to_thread(action, session, parsed)
    except (sqlite3.Error, OSError, ValueError, AttributeError):
        return _error("history_unavailable", "The bound profile's original history is unavailable")
