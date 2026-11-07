"""Durable host linkage, not a second Harness transcript or provider log.

Only construction opens a DB; there is no default path or application import.
Call recover_interrupted() once at startup, before accepting work. Opening an
additional connection deliberately does not fail another owner's running work.
Methods are synchronous: an async host should offload them to its worker thread.
Permission/cancel checks and the external effect remain the runtime's job; a
receipt cannot make an external tool exactly-once across a crash.
"""

from contextlib import contextmanager
import hashlib
import json
import math
import re
import sqlite3
import threading
import time


_TERMINAL = {"completed", "cancelled", "failed"}
_EVENT_FIELDS = {
    "type", "status", "state", "summary", "tool_name", "call_id",
    "native_event_id", "event_id", "id", "result", "error",
}
_FORBIDDEN_KEYS = {
    "apikey", "authorization", "password", "secret", "token", "accesstoken",
    "refreshtoken", "credential", "credentials", "privatekey", "headers",
    "messages", "history", "rawhistory", "rawchat", "providerlog", "providerlogs",
    "reply", "replytext", "prompt", "systemprompt", "content", "raw", "transcript",
}
_SECRET = re.compile(r"(?:\bsk-[A-Za-z0-9_-]{12,}|\bBearer\s+\S+|"
                     r"\b(?:api[_-]?key|password|secret)\s*[:=]\s*\S+)", re.I)
_THINKING_KEYS = {"thinking", "reasoning", "reasoningcontent", "thinkingcontent",
                  "chainofthought", "analysis", "thoughtsignature"}


def _context_data(value):
    """Strip model-internal fields even from receipts written by older versions.

    Tool arguments/results otherwise retain their real stored structure; this
    cannot identify reasoning disguised by a producer as an ordinary summary.
    """
    if isinstance(value, dict):
        return {k: _context_data(v) for k, v in value.items()
                if re.sub(r"[^a-z0-9]", "", k.lower()) not in _THINKING_KEYS}
    if isinstance(value, list):
        return [_context_data(v) for v in value]
    return value


def _is_tool_call(payload, excluded_name=None):
    """Exact top-level event test for SQL EXISTS, without a SQLite JSON extension.

    Parsing avoids mistaking quoted/nested types for actual calls and accepts
    older JSON whitespace/key ordering. Payloads are bounded when persisted.
    """
    try:
        event = json.loads(payload)
    except (TypeError, ValueError):
        return False
    if not isinstance(event, dict) or event.get("type") != "tool_call":
        return False
    return excluded_name is None or str(event.get("tool_name", "")).removeprefix("mcp__shikigami__") != excluded_name


def _id(value, name="id"):
    if (not isinstance(value, str) or not value.strip() or len(value) > 256
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
            or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ValueError(f"{name} must be a nonblank, control-free string of at most 256 characters")
    return value


def _limit(value):
    if type(value) is not int or not 0 <= value <= 100:
        raise ValueError("limit must be an integer between 0 and 100")
    return value


def _json(value, *, maximum=16384):
    """Validate bounded JSON and known credential/raw-transcript fields.

    This is a defensive schema boundary, not a universal secret detector.
    Callers must supply summaries and never embed credentials in free text.
    Arguments are hashed after validation, never stored as a second tool log.
    """
    nodes = 0

    def visit(item, depth):
        nonlocal nodes
        nodes += 1
        if nodes > 512 or depth > 8:
            raise ValueError("JSON exceeds the node or depth limit")
        if item is None or type(item) in (bool, int):
            if type(item) is int and not -(2**53 - 1) <= item <= 2**53 - 1:
                raise ValueError("JSON integer must be JavaScript-safe")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
        elif type(item) is str:
            if len(item) > 2048 or _SECRET.search(item):
                raise ValueError("JSON string is oversized or contains credential-like text")
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or len(key) > 128:
                    raise ValueError("JSON keys must be short strings")
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if normalized in _FORBIDDEN_KEYS or normalized.endswith(("apikey", "password", "secret")):
                    raise ValueError("credentials and raw conversation fields are not linkage data")
                visit(child, depth + 1)
        else:
            raise ValueError("value is not JSON data")

    visit(value, 0)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                             separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValueError("invalid JSON data") from error
    if size > maximum:
        raise ValueError("JSON exceeds the byte limit")
    return encoded


def _error(value):
    if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 1024):
        raise ValueError("error must be a nonblank summary of at most 1024 characters")
    _json(value)
    return value


class LinkStore:
    """Thread-safe SQLite linkage using a lock per connection and DB transactions.

    Invalid inputs, conflicts, stale runs and missing write targets raise
    ValueError; operations after close raise RuntimeError. get_run/list_runs
    include at most the latest 100 decoded events, in increasing server seq.
    is_current checks profile/revision, not whether a run is still running.
    Terminal runs retain receipts/outbox; invalidated ones cannot claim actions
    or publish results. Known late outcomes may finalize existing receipts.
    """

    def __init__(self, path):
        self._lock = threading.RLock()
        self._closed = False
        # No URI interpretation and no parent-directory creation. IDs never form paths.
        self._db = sqlite3.connect(path, timeout=10, isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.create_function("link_is_tool_call", 1, _is_tool_call, deterministic=True)
            self._db.create_function("link_is_tool_call", 2, _is_tool_call, deterministic=True)
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA synchronous=FULL")
            with self._transaction(write=True) as db:
                for statement in (
                    """CREATE TABLE IF NOT EXISTS autonomy_admissions (
                        admission_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
                        intent_hash TEXT NOT NULL, created_at REAL NOT NULL,
                        request_limit INTEGER NOT NULL)""",
                    """CREATE TABLE IF NOT EXISTS profiles (
                        profile_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0)""",
                    """CREATE TABLE IF NOT EXISTS runs (
                        ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL UNIQUE, profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                        session_id TEXT NOT NULL, origin TEXT NOT NULL, revision INTEGER NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('running','completed','cancelled','failed')),
                        stale INTEGER NOT NULL DEFAULT 0, last_seq INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT)""",
                    """CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(profile_id)
                        WHERE state='running' AND stale=0""",
                    "CREATE INDEX IF NOT EXISTS profile_runs ON runs(profile_id, ordinal)",
                    "CREATE INDEX IF NOT EXISTS session_runs ON runs(session_id)",
                    """CREATE TABLE IF NOT EXISTS events (
                        run_id TEXT NOT NULL REFERENCES runs(run_id), seq INTEGER NOT NULL,
                        native_id TEXT, payload TEXT NOT NULL, PRIMARY KEY(run_id,seq),
                        UNIQUE(run_id,native_id))""",
                    """CREATE TABLE IF NOT EXISTS actions (
                        run_id TEXT NOT NULL REFERENCES runs(run_id), call_id TEXT NOT NULL,
                        tool_name TEXT NOT NULL, args_hash TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('pending','done')),
                        result TEXT, error TEXT, PRIMARY KEY(run_id,call_id))""",
                    """CREATE TABLE IF NOT EXISTS outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL,
                        run_id TEXT NOT NULL REFERENCES runs(run_id), event_key TEXT NOT NULL,
                        payload TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(run_id,event_key))""",
                    "CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(profile_id, acknowledged, id)",
                ):
                    db.execute(statement)
                columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
                for name, declaration in (("user_message_id", "INTEGER"),
                                          ("assistant_message_id", "INTEGER"),
                                          ("parent_run_id", "TEXT")):
                    if name not in columns:
                        db.execute(f"ALTER TABLE runs ADD COLUMN {name} {declaration}")
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    @contextmanager
    def _transaction(self, *, write=False):
        with self._lock:
            if self._closed:
                raise RuntimeError("LinkStore is closed")
            self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    @staticmethod
    def _revision(db, profile_id):
        row = db.execute("SELECT revision FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
        return row[0] if row else 0

    def revision(self, profile_id):
        _id(profile_id, "profile_id")
        with self._transaction() as db:
            return self._revision(db, profile_id)

    @staticmethod
    def _run(db, run_id, *, current=False, active=False):
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("run does not exist")
        if current or active:
            revision = LinkStore._revision(db, row["profile_id"])
            if row["stale"] or row["revision"] != revision:
                raise ValueError("run is stale")
        if active and row["state"] != "running":
            raise ValueError("run is terminal")
        return row

    @staticmethod
    def _decode_run(db, row):
        result = dict(row)
        result.pop("ordinal")
        result["stale"] = bool(result["stale"])
        events = db.execute("SELECT seq,payload FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 100",
                            (row["run_id"],)).fetchall()
        result["events"] = [dict(json.loads(event["payload"]), seq=event["seq"])
                            for event in reversed(events)]
        return result

    def claim_autonomy(self, admission_id, profile_id, goal, interval, hourly_limit, request_limit):
        """Atomically reserve a rolling-hour request allowance before any work.

        Failed/unused reservations are conservative: not refunded on crashes.
        Keep only intent hashes, never a second copy of private reflections.
        """
        _id(admission_id)
        _id(profile_id)
        if not isinstance(goal, str) or not 0 < len(goal.strip()) <= 1000:
            raise ValueError('Invalid autonomous goal')
        for value, low, high in ((interval, 60, 86400), (hourly_limit, 1, 128), (request_limit, 1, 32)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError('Invalid autonomous limit')
        digest = hashlib.sha256(' '.join(goal.casefold().split()).encode()).hexdigest()
        now = time.time()
        with self._transaction(write=True) as db:
            if db.execute('SELECT 1 FROM autonomy_admissions WHERE admission_id=?', (admission_id,)).fetchone():
                return 0
            latest = db.execute('SELECT max(created_at) FROM autonomy_admissions WHERE profile_id=?', (profile_id,)).fetchone()[0]
            if latest is not None and now - latest < interval:
                return 0
            if db.execute('SELECT 1 FROM autonomy_admissions WHERE profile_id=? AND intent_hash=? AND created_at>?',
                          (profile_id, digest, now - 86400)).fetchone():
                return 0
            spent = db.execute('SELECT coalesce(sum(request_limit),0) FROM autonomy_admissions WHERE created_at>?', (now - 3600,)).fetchone()[0]
            allowance = min(request_limit, max(0, hourly_limit - spent))
            if allowance:
                db.execute('INSERT INTO autonomy_admissions VALUES (?,?,?,?,?)', (admission_id, profile_id, digest, now, allowance))
            return allowance

    def begin_run(self, run_id, profile_id, session_id, origin="user"):
        for name, value in (("run_id", run_id), ("profile_id", profile_id), ("session_id", session_id)):
            _id(value, name)
        if origin not in ("user", "reflection"):
            raise ValueError("origin must be user or reflection")
        with self._transaction(write=True) as db:
            db.execute("INSERT OR IGNORE INTO profiles(profile_id) VALUES (?)", (profile_id,))
            revision = self._revision(db, profile_id)
            # A fresh run ID must not resurrect deleted context in an old session,
            # nor attach another profile to that session's private history.
            previous = db.execute("""SELECT 1 FROM runs WHERE session_id=?
                AND (profile_id<>? OR revision<>? OR stale=1) LIMIT 1""",
                                  (session_id, profile_id, revision)).fetchone()
            if previous:
                raise ValueError("session belongs to a different profile or stale revision")
            now = time.time()
            try:
                db.execute("""INSERT INTO runs
                    (run_id,profile_id,session_id,origin,revision,state,created_at,updated_at)
                    VALUES (?,?,?,?,?,'running',?,?)""",
                           (run_id, profile_id, session_id, origin, revision, now, now))
            except sqlite3.IntegrityError as error:
                raise ValueError("duplicate run id or profile already has an active run") from error
            return self._decode_run(db, self._run(db, run_id))

    def get_run(self, run_id, profile_id=None):
        _id(run_id, "run_id")
        if profile_id is not None:
            _id(profile_id, "profile_id")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or (profile_id is not None and row["profile_id"] != profile_id):
                return None
            return self._decode_run(db, row)

    def bind_messages(self, run_id, user_message_id=None, assistant_message_id=None):
        """Bind chat DB IDs without copying speech. Return the decoded run.

        None leaves a field unchanged. Same-value retries are harmless; rebinding
        or stale writes raise ValueError. The host must verify these IDs belong
        to the run's profile chat DB and expected roles: there is no cross-DB FK
        or atomic commit between the two stores.
        """
        _id(run_id, "run_id")
        values = {"user_message_id": user_message_id, "assistant_message_id": assistant_message_id}
        for name, value in values.items():
            if value is not None and (type(value) is not int or not 0 < value <= 2**63 - 1):
                raise ValueError(f"{name} must be a positive SQLite integer or None")
        with self._transaction(write=True) as db:
            run = self._run(db, run_id, current=True)
            for name, value in values.items():
                if value is not None and run[name] is not None and run[name] != value:
                    raise ValueError(f"{name} is already bound")
            changes = {name: value for name, value in values.items()
                       if value is not None and run[name] is None}
            if changes:
                assignments = ",".join(f"{name}=?" for name in changes)
                db.execute(f"UPDATE runs SET {assignments},updated_at=? WHERE run_id=?",
                           (*changes.values(), time.time(), run_id))
            return self._decode_run(db, self._run(db, run_id))

    def set_parent(self, run_id, parent_run_id):
        """Link a continuation to an older terminal run; never resume a process.

        Both runs must be current, in the same profile/revision. Requiring the
        parent's ordinal to precede the child prevents self-links and cycles.
        Same-parent retries are harmless; replacing a parent is forbidden.
        """
        _id(run_id, "run_id")
        _id(parent_run_id, "parent_run_id")
        with self._transaction(write=True) as db:
            child = self._run(db, run_id, current=True)
            parent = self._run(db, parent_run_id, current=True)
            if (parent["profile_id"] != child["profile_id"]
                    or parent["revision"] != child["revision"]):
                raise ValueError("parent belongs to a different profile or revision")
            if parent["state"] not in _TERMINAL:
                raise ValueError("parent run is active")
            if parent["ordinal"] >= child["ordinal"]:
                raise ValueError("parent must precede child")
            if child["parent_run_id"] not in (None, parent_run_id):
                raise ValueError("parent is already bound")
            if child["parent_run_id"] is None:
                db.execute("UPDATE runs SET parent_run_id=?,updated_at=? WHERE run_id=?",
                           (parent_run_id, time.time(), run_id))
            return self._decode_run(db, self._run(db, run_id))

    def task_context(self, profile_id, run_id="") -> dict:
        """Read bounded evidence for a new task, without claims of process resumption.

        Empty run_id selects the newest current terminal user-origin task,
        returning {} if none. Completed chats qualify only with a tool_call,
        action receipt or parent link; cancelled/failed runs remain eligible.
        Selection checks all stored evidence, independent of the returned tail.
        Legacy runs may lack message IDs. Explicit run IDs also permit ordinary
        completed chats, retaining targeted continuation semantics. An explicit missing,
        foreign, stale, running or reflection run raises ValueError.
        events contains tool_call/tool_result projections from the latest 100 events;
        actions contains at most 100 receipts, newest first then chronological.
        A pending receipt is exposed as unknown, including recorded failures:
        neither a failed task nor a lost response proves no external effect.
        Truncation flags mean absence is not evidence of no prior action.
        This read does not acknowledge outbox entries or execute/replay anything.
        """
        _id(profile_id, "profile_id")
        if run_id != "":
            _id(run_id, "run_id")
        with self._transaction() as db:
            if run_id:
                run = self._run(db, run_id, current=True)
                if run["profile_id"] != profile_id:
                    raise ValueError("run belongs to another profile")
                if run["state"] not in _TERMINAL or run["origin"] != "user":
                    raise ValueError("run is not a terminal visible task")
            else:
                run = db.execute("""SELECT r.* FROM runs r JOIN profiles p USING(profile_id)
                    WHERE r.profile_id=? AND r.stale=0 AND r.revision=p.revision
                        AND r.state IN ('completed','cancelled','failed') AND r.origin='user'
                        AND (r.state IN ('cancelled','failed') OR r.parent_run_id IS NOT NULL
                            OR EXISTS (SELECT 1 FROM actions a WHERE a.run_id=r.run_id)
                            OR EXISTS (SELECT 1 FROM events e WHERE e.run_id=r.run_id
                                AND link_is_tool_call(e.payload)))
                    ORDER BY r.ordinal DESC LIMIT 1""", (profile_id,)).fetchone()
                if run is None:
                    return {}
            context = {key: run[key] for key in (
                "run_id", "profile_id", "session_id", "revision", "origin", "state",
                "created_at", "updated_at", "error", "user_message_id",
                "assistant_message_id", "parent_run_id")}
            # Bound the read as well as the output; no SQLite JSON extension.
            # A truncated tail must never be interpreted as complete evidence.
            rows = db.execute("SELECT seq,payload FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 101",
                              (run["run_id"],)).fetchall()
            events = []
            for row in reversed(rows[:100]):
                event = json.loads(row["payload"])
                if event.get("type") in ("tool_call", "tool_result"):
                    events.append(dict(_context_data(event), seq=row["seq"]))
            context["events_truncated"] = len(rows) > 100
            context["events"] = events
            rows = db.execute("""SELECT call_id,tool_name,status,result,error FROM actions
                WHERE run_id=? ORDER BY rowid DESC LIMIT 101""", (run["run_id"],)).fetchall()
            context["actions_truncated"] = len(rows) > 100
            context["actions"] = [dict(
                call_id=row["call_id"], tool_name=row["tool_name"],
                status="done" if row["status"] == "done" else "unknown",
                result=_context_data(json.loads(row["result"])) if row["result"] is not None else None,
                error=row["error"],
            ) for row in reversed(rows[:100])]
            return context

    def all_run_ids(self, profile_id):
        """Cleanup inventory, intentionally independent of the UI page limit."""
        _id(profile_id, 'profile_id')
        with self._transaction() as db:
            return [row['run_id'] for row in db.execute(
                'SELECT run_id FROM runs WHERE profile_id=? ORDER BY ordinal', (profile_id,))]

    def task_search_records(self, profile_id, limit):
        """Read task provenance and the terminal projection for keyword lookup."""
        _id(profile_id, "profile_id")
        with self._transaction() as db:
            rows = db.execute("""
                SELECT r.run_id, r.origin, r.state, r.created_at, r.user_message_id,
                       e.payload AS terminal_event, o.payload AS outcome
                FROM runs r JOIN profiles p USING (profile_id)
                LEFT JOIN events e ON e.run_id=r.run_id AND e.seq=r.last_seq
                LEFT JOIN outbox o ON o.run_id=r.run_id AND o.event_key='task-finished'
                WHERE r.profile_id=? AND r.stale=0 AND r.revision=p.revision
                    AND r.state IN ('completed','cancelled','failed')
                    AND (r.origin='reflection' OR r.state IN ('cancelled','failed')
                        OR r.parent_run_id IS NOT NULL
                        OR EXISTS (SELECT 1 FROM actions a WHERE a.run_id=r.run_id)
                        OR EXISTS (SELECT 1 FROM events t WHERE t.run_id=r.run_id
                            AND link_is_tool_call(t.payload, 'task_search')))
                ORDER BY r.ordinal DESC LIMIT ?
            """, (profile_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def list_runs(self, profile_id, limit=30):
        _id(profile_id, "profile_id")
        _limit(limit)
        with self._transaction() as db:
            rows = db.execute("SELECT * FROM runs WHERE profile_id=? ORDER BY ordinal DESC LIMIT ?",
                              (profile_id, limit)).fetchall()
            return [self._decode_run(db, row) for row in rows]

    def append_event(self, run_id, event):
        """Accept a projected event, never raw SDK events or reply deltas.

        Allowed fields: type, status, state, summary, tool_name, call_id,
        native_event_id (aliases event_id/id), result and error. Result must be
        bounded JSON; summary/error are at most 1024 chars. Native ID aliases
        must agree. seq is assigned here, and duplicate IDs must match payload.
        """
        _id(run_id, "run_id")
        if type(event) is not dict or not event or set(event) - _EVENT_FIELDS:
            raise ValueError("event must contain only projected status/tool fields")
        _id(event.get("type"), "event type")
        event = dict(event)
        native_ids = [event.pop(key) for key in ("native_event_id", "event_id", "id") if key in event]
        for value in native_ids:
            _id(value, "native event id")
        if native_ids and any(value != native_ids[0] for value in native_ids):
            raise ValueError("native event id aliases disagree")
        native_id = native_ids[0] if native_ids else None
        if native_id is not None:
            event["native_event_id"] = native_id
        for key in ("status", "state", "tool_name", "call_id"):
            if key in event:
                _id(event[key], key)
        for key in ("summary", "error"):
            if key in event:
                _error(event[key])
        payload = _json(event, maximum=8192)
        with self._transaction(write=True) as db:
            run = self._run(db, run_id, current=True)
            if native_id is not None:
                old = db.execute("SELECT seq,payload FROM events WHERE run_id=? AND native_id=?",
                                 (run_id, native_id)).fetchone()
                if old:
                    if old["payload"] != payload:
                        raise ValueError("native event id reused with different data")
                    return dict(json.loads(old["payload"]), seq=old["seq"])
            if run["state"] != "running":
                raise ValueError("run is terminal")
            seq = run["last_seq"] + 1
            db.execute("INSERT INTO events(run_id,seq,native_id,payload) VALUES (?,?,?,?)",
                       (run_id, seq, native_id, payload))
            db.execute("UPDATE runs SET last_seq=?,updated_at=? WHERE run_id=?", (seq, time.time(), run_id))
            return dict(json.loads(payload), seq=seq)

    def finish_run(self, run_id, state, error=None, *, publish_result=False, model_requests=0, outcome=None):
        """Finish a run, optionally publishing its bounded summary atomically."""
        _id(run_id, "run_id")
        if not isinstance(state, str) or state not in _TERMINAL:
            raise ValueError("state must be completed, cancelled or failed")
        _error(error)
        if type(model_requests) is not int or model_requests < 0:
            raise ValueError("model_requests must be a nonnegative integer")
        if publish_result:
            result = {"state": state, "model_requests": model_requests}
            if outcome is not None:
                result['outcome'] = outcome
            if error is not None:
                result["error"] = error
            payload = _json({"tool": "agent_task", "result": result})
        with self._transaction(write=True) as db:
            run = self._run(db, run_id, current=True)
            if run["state"] != "running":
                if run["state"] != state or run["error"] != error:
                    raise ValueError("terminal outcome cannot be changed")
            else:
                db.execute("UPDATE runs SET state=?,error=?,updated_at=? WHERE run_id=?",
                           (state, error, time.time(), run_id))
            if publish_result:
                self._enqueue_result(db, run["profile_id"], run_id, "task-finished", payload)
            return self._decode_run(db, self._run(db, run_id))

    def invalidate(self, profile_id):
        """Advance revision and tombstone all previous runs in one transaction."""
        _id(profile_id, "profile_id")
        with self._transaction(write=True) as db:
            db.execute("INSERT OR IGNORE INTO profiles(profile_id) VALUES (?)", (profile_id,))
            db.execute("UPDATE profiles SET revision=revision+1 WHERE profile_id=?", (profile_id,))
            active = [row[0] for row in db.execute(
                "SELECT run_id FROM runs WHERE profile_id=? AND state='running' AND stale=0 ORDER BY ordinal",
                (profile_id,))]
            db.execute("""UPDATE runs SET stale=1,
                state=CASE WHEN state='running' THEN 'cancelled' ELSE state END,
                updated_at=? WHERE profile_id=? AND stale=0""", (time.time(), profile_id))
            db.execute("UPDATE outbox SET acknowledged=1 WHERE profile_id=?", (profile_id,))
            return self._revision(db, profile_id), active

    def is_current(self, run_id, profile_id):
        _id(run_id, "run_id")
        _id(profile_id, "profile_id")
        with self._transaction() as db:
            row = db.execute("""SELECT 1 FROM runs r JOIN profiles p ON p.profile_id=r.profile_id
                WHERE r.run_id=? AND r.profile_id=? AND r.stale=0 AND r.revision=p.revision""",
                             (run_id, profile_id)).fetchone()
            return row is not None

    def recover_interrupted(self):
        """Startup-only: fail persisted running runs, never replay pending actions."""
        with self._transaction(write=True) as db:
            active = [row[0] for row in db.execute(
                "SELECT run_id FROM runs WHERE state='running' AND stale=0 ORDER BY ordinal")]
            db.execute("""UPDATE runs SET state='failed',error='interrupted; effects may be unknown',
                updated_at=? WHERE state='running' AND stale=0""", (time.time(),))
            for run_id in active:
                row = self._run(db, run_id, current=True)
                self._enqueue_result(db, row['profile_id'], run_id, 'task-finished', _json({
                    'tool': 'agent_task', 'result': {'state': 'failed', 'error': 'interrupted; effects may be unknown'}}))
            return active

    def claim_action(self, run_id, call_id, tool_name, args):
        """Claim once per run/call; existing pending means DO NOT execute again."""
        for name, value in (("run_id", run_id), ("call_id", call_id), ("tool_name", tool_name)):
            _id(value, name)
        if type(args) is not dict:
            raise ValueError("args must be a JSON object")
        digest = hashlib.sha256(_json(args).encode("utf-8")).hexdigest()
        with self._transaction(write=True) as db:
            run = self._run(db, run_id, current=True)
            row = db.execute("SELECT * FROM actions WHERE run_id=? AND call_id=?", (run_id, call_id)).fetchone()
            if row:
                if row["tool_name"] != tool_name or row["args_hash"] != digest:
                    raise ValueError("call id reused with different tool or arguments")
                if row["status"] == "done":
                    return {"status": "done", "result": json.loads(row["result"])}
                return dict(status="pending", **({"error": row["error"]} if row["error"] else {}))
            if run["state"] != "running":
                raise ValueError("run is terminal")
            db.execute("""INSERT INTO actions(run_id,call_id,tool_name,args_hash,status)
                VALUES (?,?,?,?,'pending')""", (run_id, call_id, tool_name, digest))
            return {"status": "new"}

    def complete_action(self, run_id, call_id, result, *, publish_result=False):
        """Record known success; optionally publish in the same transaction.

        Default receipt-only completion permits late results after invalidation.
        Publication requires a current run and always uses its bound profile.
        """
        _id(run_id, "run_id")
        _id(call_id, "call_id")
        payload = _json(result)
        with self._transaction(write=True) as db:
            row = db.execute("SELECT * FROM actions WHERE run_id=? AND call_id=?", (run_id, call_id)).fetchone()
            if row is None:
                raise ValueError("action was not claimed")
            if row["status"] == "done" and row["result"] != payload:
                raise ValueError("action result cannot be changed")
            db.execute("UPDATE actions SET status='done',result=?,error=NULL WHERE run_id=? AND call_id=?",
                       (payload, run_id, call_id))
            if publish_result:
                run = self._run(db, run_id, current=True)
                encoded = _json({"tool": row["tool_name"], "result": json.loads(payload)})
                self._enqueue_result(db, run["profile_id"], run_id, call_id, encoded)
            return {"status": "done", "result": json.loads(payload)}

    def fail_action(self, run_id, call_id, error):
        """Persist uncertainty; failure does not release the action for replay."""
        _id(run_id, "run_id")
        _id(call_id, "call_id")
        _error(error)
        if error is None:
            raise ValueError("failed action requires an error summary")
        with self._transaction(write=True) as db:
            row = db.execute("SELECT status FROM actions WHERE run_id=? AND call_id=?", (run_id, call_id)).fetchone()
            if row is None or row["status"] == "done":
                raise ValueError("action is missing or already completed")
            db.execute("UPDATE actions SET error=? WHERE run_id=? AND call_id=?", (error, run_id, call_id))
            return {"status": "pending", "error": error}

    def find_timer_run(self, profile_id: str, timer_id: str) -> str | None:
        """Find the newest current run with a completed timer_create receipt.

        Pure lookup for expiry callbacks, including terminal runs whose creation
        result was already acknowledged by reflection. Outbox state is irrelevant.
        A stale run or a receipt belonging to another profile can never match.
        """
        _id(profile_id, "profile_id")
        _id(timer_id, "timer_id")
        with self._transaction() as db:
            rows = db.execute("""SELECT a.run_id,a.result FROM actions a
                JOIN runs r ON r.run_id=a.run_id
                JOIN profiles p ON p.profile_id=r.profile_id
                WHERE r.profile_id=? AND r.stale=0 AND r.revision=p.revision
                    AND a.status='done' AND a.tool_name='timer_create'
                ORDER BY r.ordinal DESC""", (profile_id,))
            # Decode here to avoid requiring SQLite's optional JSON extension.
            for row in rows:
                result = json.loads(row["result"])
                if isinstance(result, dict) and result.get("timer_id") == timer_id:
                    return row["run_id"]
            return None

    def enqueue_result(self, profile_id, run_id, event_key, payload):
        """Deduplicate one bounded reflection result per run/event; return row ID."""
        for name, value in (("profile_id", profile_id), ("run_id", run_id), ("event_key", event_key)):
            _id(value, name)
        if type(payload) is not dict:
            raise ValueError("result payload must be a JSON object")
        encoded = _json(payload)
        with self._transaction(write=True) as db:
            return self._enqueue_result(db, profile_id, run_id, event_key, encoded)

    def _enqueue_result(self, db, profile_id, run_id, event_key, encoded):
        """Enqueue validated JSON using the caller's transaction and dedup rules."""
        run = self._run(db, run_id, current=True)
        if run["profile_id"] != profile_id:
            raise ValueError("run belongs to another profile")
        old = db.execute("SELECT id,payload FROM outbox WHERE run_id=? AND event_key=?",
                         (run_id, event_key)).fetchone()
        if old:
            if old["payload"] != encoded:
                raise ValueError("event key reused with different result")
            return old["id"]
        cursor = db.execute("INSERT INTO outbox(profile_id,run_id,event_key,payload) VALUES (?,?,?,?)",
                            (profile_id, run_id, event_key, encoded))
        return cursor.lastrowid

    def peek_results(self, profile_id, limit=30):
        """Pure read; the caller acknowledges only after reflection commits."""
        _id(profile_id, "profile_id")
        _limit(limit)
        with self._transaction() as db:
            rows = db.execute("""SELECT o.id,o.payload FROM outbox o
                JOIN runs r ON r.run_id=o.run_id JOIN profiles p ON p.profile_id=r.profile_id
                WHERE o.profile_id=? AND o.acknowledged=0 AND r.stale=0 AND r.revision=p.revision
                ORDER BY o.id LIMIT ?""", (profile_id, limit)).fetchall()
            return [{"id": row["id"], "payload": json.loads(row["payload"])} for row in rows]

    def acknowledge_results(self, ids):
        """Mark delivered, retaining dedup tombstones even after acknowledgment."""
        if not isinstance(ids, (list, tuple)) or len(ids) > 100:
            raise ValueError("ids must be a list of at most 100 outbox IDs")
        if any(type(value) is not int or not 0 < value <= 2**63 - 1 for value in ids):
            raise ValueError("outbox IDs must be positive SQLite integers")
        with self._transaction(write=True) as db:
            db.executemany("UPDATE outbox SET acknowledged=1 WHERE id=?", [(value,) for value in ids])

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True
