import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _validate_provenance(agent_run_id, generation_status):
    if not isinstance(generation_status, str) or generation_status not in ("completed", "cancelled", "failed"):
        raise ValueError("generation_status must be completed, cancelled or failed")
    if agent_run_id is not None and (
        not isinstance(agent_run_id, str) or not agent_run_id.strip() or len(agent_run_id) > 256
        or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in agent_run_id)
    ):
        raise ValueError("agent_run_id must be a nonblank, control-free ID or None")


@dataclass
class Message:
    role: str       # "user" | "assistant" | "system"
    content: str
    timestamp: float = 0.0
    sender: str = ""  # 发言者标识：用户名或人格 display_name，群聊防污染用
    agent_run_id: Optional[str] = None
    generation_status: str = "completed"
    id: Optional[int] = None

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    def to_llm_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


class ConversationStore:
    """Stores conversation history in a SQLite database under storage_root.

    max_turns=0  → never trim (permanent memory, use for profile-level store).
    max_turns>0  → keep at most max_turns*2 messages (session-level store).

    get_recent() returns the last n_turns rounds, filtered by context_since_ts:
      - Messages with timestamp < context_since_ts are excluded from LLM context
        but remain in the DB so they still appear in the chat UI.
    reset_context() sets context_since_ts = now (non-destructive short-mem clear).
    clear() wipes everything including the reset marker.

    Auto-migrates from chat_records.json on first open (renames to .json.bak).
    """

    def __init__(self, storage_root: str, max_turns: int = 60):
        self.storage_root = storage_root
        self._db_path = os.path.join(storage_root, "chat_records.db")
        self._ctx_path = os.path.join(storage_root, "context_reset.json")
        self.max_turns = max_turns
        self._context_since_ts: float = 0.0
        os.makedirs(storage_root, exist_ok=True)
        self._conn = self._init_db()
        self._load_ctx_ts()

    def close(self) -> None:
        """Close the DB connection so the DB file can be deleted (e.g. when removing a group)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── DB initialisation & migration ────────────────────────────────────────

    def _init_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                role      TEXT    NOT NULL,
                content   TEXT    NOT NULL,
                timestamp REAL    NOT NULL,
                sender    TEXT    NOT NULL DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON messages(timestamp)")
        conn.commit()

        # Additive migration, serialized across connections; old rows are completed.
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
                for name, declaration in (
                    ("sender", "TEXT NOT NULL DEFAULT ''"),
                    ("agent_run_id", "TEXT"),
                    ("generation_status", "TEXT NOT NULL DEFAULT 'completed' "
                     "CHECK(generation_status IN ('completed','cancelled','failed'))"),
                ):
                    if name not in columns:
                        conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")
        except BaseException:
            conn.close()
            raise

        # Auto-migrate from JSON if .bak doesn't already exist
        json_path = os.path.join(self.storage_root, "chat_records.json")
        bak_path  = os.path.join(self.storage_root, "chat_records.json.bak")
        if os.path.exists(json_path) and not os.path.exists(bak_path):
            self._migrate_from_json(conn, json_path, bak_path)

        return conn

    def _migrate_from_json(self, conn: sqlite3.Connection,
                           json_path: str, bak_path: str) -> None:
        import json as _json
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            rows = [
                (m.get("role", ""), m.get("content", ""),
                 float(m.get("timestamp") or time.time()),
                 m.get("sender", "") or ("用户" if m.get("role") == "user" else ""))
                for m in data if m.get("role") and m.get("content")
            ]
            with conn:
                conn.executemany(
                    "INSERT INTO messages (role, content, timestamp, sender) VALUES (?, ?, ?, ?)",
                    rows
                )
            os.rename(json_path, bak_path)
            logger.info("[ConversationStore] migrated %d messages from JSON → SQLite (%s)",
                        len(rows), self._db_path)
        except Exception as e:
            logger.warning("[ConversationStore] JSON migration failed: %s", e)

    # ── Context reset persistence (context_reset.json stays as-is) ───────────

    def _load_ctx_ts(self):
        if not os.path.exists(self._ctx_path):
            return
        try:
            import json as _json
            with open(self._ctx_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            self._context_since_ts = float(data.get("since_ts", 0.0))
        except Exception:
            self._context_since_ts = 0.0

    def _save_ctx_ts(self):
        try:
            import json as _json
            with open(self._ctx_path, "w", encoding="utf-8") as f:
                _json.dump({"since_ts": self._context_since_ts}, f)
        except Exception as e:
            logger.warning("[ConversationStore] save context_ts failed: %s", e)

    # ── Write operations ─────────────────────────────────────────────────────

    def append(self, role: str, content: str, sender: str = "", *,
               agent_run_id: Optional[str] = None, generation_status: str = "completed") -> int:
        """Persist speech unchanged; provenance is metadata, never a speech prefix."""
        _validate_provenance(agent_run_id, generation_status)
        ts = time.time()
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO messages (role, content, timestamp, sender, agent_run_id, generation_status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (role, content, ts, (sender or "").strip(), agent_run_id, generation_status)
            )
        message_id = int(cur.lastrowid)
        if self.max_turns > 0:
            limit = self.max_turns * 2
            with self._conn:
                self._conn.execute("""
                    DELETE FROM messages WHERE id NOT IN (
                        SELECT id FROM messages ORDER BY timestamp DESC, id DESC LIMIT ?
                    )
                """, (limit,))
        return message_id

    def bind_user_task(self, message_id: int, run_id: str) -> None:
        _validate_provenance(run_id, "completed")
        with self._conn:
            updated = self._conn.execute(
                "UPDATE messages SET agent_run_id=? WHERE id=? AND role='user' AND agent_run_id IS NULL",
                (run_id, message_id),
            )
            if updated.rowcount != 1:
                raise ValueError("Task request message is no longer available")

    def reset_context(self):
        """Mark current time as the context start point (non-destructive)."""
        self._context_since_ts = time.time()
        self._save_ctx_ts()

    def clear(self):
        """Wipe all messages and remove the context-reset marker."""
        with self._conn:
            self._conn.execute("DELETE FROM messages")
        self._context_since_ts = 0.0
        if os.path.exists(self._ctx_path):
            try:
                os.remove(self._ctx_path)
            except Exception:
                pass

    def save_all(self, messages: List[Message]) -> None:
        """Replace messages, retaining IDs/provenance on previously read messages."""
        rows = []
        for m in messages:
            run_id = getattr(m, "agent_run_id", None)
            status = getattr(m, "generation_status", "completed")
            _validate_provenance(run_id, status)
            rows.append((getattr(m, "id", None), m.role, m.content, m.timestamp,
                         getattr(m, "sender", "") or "", run_id, status))
        with self._conn:
            self._conn.execute("DELETE FROM messages")
            self._conn.executemany(
                "INSERT INTO messages (id, role, content, timestamp, sender, agent_run_id, generation_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", rows
            )

    def delete_from_index(self, idx: int) -> bool:
        """Delete the idx-th message in time order (0-based, same order as get_all) and everything after it."""
        if idx < 0:
            return False
        with self._conn:
            cur = self._conn.execute("""
                DELETE FROM messages WHERE id IN (
                    SELECT id FROM messages ORDER BY timestamp, id LIMIT -1 OFFSET ?
                )
            """, (idx,))
        return cur.rowcount > 0

    def delete_at_index(self, idx: int) -> bool:
        """Delete the idx-th message in time order (0-based, same order as get_all)."""
        if idx < 0:
            return False
        with self._conn:
            cur = self._conn.execute("""
                DELETE FROM messages WHERE id IN (
                    SELECT id FROM messages ORDER BY timestamp, id LIMIT 1 OFFSET ?
                )
            """, (idx,))
        return cur.rowcount > 0

    # ── Read operations ──────────────────────────────────────────────────────

    def get_recent(self, n_turns: int = 20) -> List[Dict]:
        """Return recent speech with IDs and structured generation metadata.

        Excludes messages with timestamp < context_since_ts so that a short-term
        memory reset is respected without deleting visible chat history.
        Includes incomplete AI replies unchanged for compatibility. Memory and
        summary consumers must inspect generation_status before flattening
        content; history prompts render it separately as trusted context.
        """
        since = self._context_since_ts
        rows = self._conn.execute("""
            SELECT role, content, sender, id, agent_run_id, generation_status FROM messages
            WHERE role IN ('user', 'assistant')
              AND (? = 0 OR timestamp >= ?)
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
        """, (since, since, n_turns * 2)).fetchall()
        rows.reverse()
        return [{"role": r[0], "content": r[1], "sender": r[2] or "",
                 "id": r[3], "agent_run_id": r[4], "generation_status": r[5]} for r in rows]

    def memory_tail_id(self) -> int:
        row = self._conn.execute("SELECT MAX(id) FROM messages").fetchone()
        return int(row[0] or 0)

    def memory_messages_after(self, after_id: int, limit: int) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT id, role, content, sender, timestamp, generation_status FROM messages "
            "WHERE id > ? AND timestamp >= ? ORDER BY id LIMIT ?",
            (after_id, self.context_since_ts, limit),
        ).fetchall()
        return [dict(zip(("id", "role", "content", "sender", "timestamp", "generation_status"), row))
                for row in rows]

    def has_memory_evidence(self, message: Dict) -> bool:
        row = self._conn.execute(
            "SELECT role, content, sender, timestamp FROM messages WHERE id=? AND timestamp >= ?",
            (message["id"], self.context_since_ts),
        ).fetchone()
        return row is not None and tuple(row) == tuple(message[key] for key in ("role", "content", "sender", "timestamp"))

    def get_recent_with_ts(self, n_turns: int = 20) -> List[Dict]:
        """Like get_recent but each dict includes 'timestamp' for merging with group messages."""
        since = self._context_since_ts
        rows = self._conn.execute("""
            SELECT role, content, sender, timestamp, id, agent_run_id, generation_status FROM messages
            WHERE role IN ('user', 'assistant')
              AND (? = 0 OR timestamp >= ?)
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
        """, (since, since, n_turns * 2)).fetchall()
        rows.reverse()
        return [
            {"role": r[0], "content": r[1], "sender": r[2] or "", "timestamp": r[3],
             "id": r[4], "agent_run_id": r[5], "generation_status": r[6]}
            for r in rows
        ]

    def get_recent_by_sender(self, n: int = 10, sender: str = "") -> List[Dict]:
        """Return the last n messages where sender matches (e.g. 群聊按发言人召回)."""
        if not (sender and sender.strip()):
            return []
        s = sender.strip()
        rows = self._conn.execute("""
            SELECT role, content, sender, id, agent_run_id, generation_status FROM messages
            WHERE sender = ? AND role IN ('user', 'assistant')
            ORDER BY timestamp DESC, id DESC LIMIT ?
        """, (s, n)).fetchall()
        rows.reverse()
        return [{"role": r[0], "content": r[1], "sender": r[2] or "",
                 "id": r[3], "agent_run_id": r[4], "generation_status": r[5]} for r in rows]

    def get_recent_since(self, since_ts: float, limit: int = 50) -> List[Dict]:
        """Return up to `limit` messages with timestamp >= since_ts (all senders), chronological order.
        Used for 单聊合并群近期：merged_history 在短期上下文时间窗内取群消息。
        """
        items = self.get_recent_since_with_ts(since_ts, limit)
        return [{k: v for k, v in m.items() if k != "timestamp"} for m in items]

    def get_recent_since_with_ts(self, since_ts: float, limit: int = 50) -> List[Dict]:
        """Like get_recent_since but each dict includes 'timestamp' for merging (单聊+群聊合并)."""
        if limit <= 0:
            return []
        rows = self._conn.execute("""
            SELECT role, content, sender, timestamp, id, agent_run_id, generation_status FROM messages
            WHERE role IN ('user', 'assistant') AND timestamp >= ?
            ORDER BY timestamp DESC, id DESC LIMIT ?
        """, (max(0.0, since_ts), max(1, limit))).fetchall()
        rows.reverse()
        return [
            {"role": r[0], "content": r[1], "sender": r[2] or "", "timestamp": r[3],
             "id": r[4], "agent_run_id": r[5], "generation_status": r[6]}
            for r in rows
        ]

    def get_all(self) -> List[Message]:
        """Return all stored messages (for history API / client sync).

        Heavy on large tables (reads everything and builds an object per row); new code should use
        SQL-level queries such as get_last / get_page_with_id / count_all instead of slicing this.
        """
        rows = self._conn.execute(
            "SELECT role, content, timestamp, sender, id, agent_run_id, generation_status FROM messages ORDER BY timestamp, id"
        ).fetchall()
        return [
            Message(role=r[0], content=r[1], timestamp=r[2], sender=r[3] or "",
                    id=r[4], agent_run_id=r[5], generation_status=r[6])
            for r in rows
        ]

    def get_all_with_id(self) -> List[Dict]:
        """Return all messages as dicts with id, role, content, timestamp, sender (for paginated history)."""
        rows = self._conn.execute(
            "SELECT id, role, content, timestamp, sender, agent_run_id, generation_status FROM messages ORDER BY timestamp, id"
        ).fetchall()
        return [
            {"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3], "sender": r[4] or "",
             "agent_run_id": r[5], "generation_status": r[6]}
            for r in rows
        ]

    def count_all(self) -> int:
        """Total message count (used by the history API instead of len(get_all()))."""
        row = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return row[0] if row else 0

    def get_last(self, limit: int) -> List[Message]:
        """The last `limit` messages in chronological order; limit <= 0 is the same as get_all()."""
        if limit <= 0:
            return self.get_all()
        rows = self._conn.execute("""
            SELECT role, content, timestamp, sender, id, agent_run_id, generation_status FROM messages
            ORDER BY timestamp DESC, id DESC LIMIT ?
        """, (limit,)).fetchall()
        rows.reverse()
        return [
            Message(role=r[0], content=r[1], timestamp=r[2], sender=r[3] or "",
                    id=r[4], agent_run_id=r[5], generation_status=r[6])
            for r in rows
        ]

    def get_page_with_id(self, limit: int, offset: int = 0) -> List[Dict]:
        """Page backwards from the newest (offset=0 is the newest page); returns dicts with id, in chronological order."""
        if limit <= 0:
            return self.get_all_with_id()
        rows = self._conn.execute("""
            SELECT id, role, content, timestamp, sender, agent_run_id, generation_status FROM messages
            ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?
        """, (limit, max(0, offset))).fetchall()
        rows.reverse()
        return [
            {"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3], "sender": r[4] or "",
             "agent_run_id": r[5], "generation_status": r[6]}
            for r in rows
        ]

    def has_messages_between(self, start_ts: float, end_ts: float) -> bool:
        """Whether any user/assistant message exists in [start_ts, end_ts) (used by the yesterday-summary check)."""
        row = self._conn.execute("""
            SELECT 1 FROM messages
            WHERE role IN ('user', 'assistant') AND timestamp >= ? AND timestamp < ?
            LIMIT 1
        """, (start_ts, end_ts)).fetchone()
        return row is not None

    def count_since(self, since_ts: float, cap: int = 0) -> int:
        """Number of user/assistant messages with timestamp > since_ts.

        With cap > 0, counting stops at cap, so callers that only need to know whether a threshold
        was reached don't scan the whole table.
        """
        if cap > 0:
            row = self._conn.execute("""
                SELECT COUNT(*) FROM (
                    SELECT 1 FROM messages
                    WHERE role IN ('user', 'assistant') AND timestamp > ?
                    LIMIT ?
                )
            """, (since_ts, cap)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role IN ('user', 'assistant') AND timestamp > ?",
                (since_ts,)
            ).fetchone()
        return row[0] if row else 0

    def search_messages(self, q: str = "", limit: int = 50,
                        offset: int = 0) -> List[Dict]:
        """Search messages by content (LIKE), newest first, paginated.

        Returns list of dicts with keys: id, role, content, timestamp.
        Empty q returns all messages (newest first).
        """
        pattern = f"%{q}%" if q else "%"
        rows = self._conn.execute("""
            SELECT id, role, content, timestamp, sender, agent_run_id, generation_status FROM messages
            WHERE content LIKE ?
            ORDER BY timestamp DESC, id DESC
            LIMIT ? OFFSET ?
        """, (pattern, limit, offset)).fetchall()
        return [
            {"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3], "sender": r[4] or "",
             "agent_run_id": r[5], "generation_status": r[6]}
            for r in rows
        ]

    def delete_by_id(self, msg_id: int) -> bool:
        """Delete a single message by its SQLite row id. Returns True if deleted."""
        with self._conn:
            cur = self._conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
        return cur.rowcount > 0

    def count_messages(self, q: str = "") -> int:
        """Count messages matching q (empty = all)."""
        pattern = f"%{q}%" if q else "%"
        row = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content LIKE ?", (pattern,)
        ).fetchone()
        return row[0] if row else 0

    def last_user_message_time(self, human_sender: Optional[str] = None) -> Optional[float]:
        """Return timestamp of the most recent human message, or None.
        When human_sender is set (e.g. config.user_name), match sender=human_sender OR
        (role='user' AND empty sender) so legacy rows without sender are included.
        """
        if human_sender is not None and str(human_sender).strip():
            row = self._conn.execute(
                """SELECT timestamp FROM messages
                   WHERE sender = ? OR (role = 'user' AND (sender = '' OR sender IS NULL))
                   ORDER BY timestamp DESC, id DESC LIMIT 1""",
                (human_sender.strip(),),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT timestamp FROM messages WHERE role='user' ORDER BY timestamp DESC, id DESC LIMIT 1"
            ).fetchone()
        return row[0] if row else None

    def first_message_time(self) -> Optional[float]:
        """Return timestamp of the very first stored message, or None."""
        row = self._conn.execute(
            "SELECT timestamp FROM messages ORDER BY timestamp LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def count_user_messages_since(self, since_ts: float, human_sender: Optional[str] = None) -> int:
        """Count human messages with timestamp >= since_ts. When human_sender set, includes
        sender=human_sender OR (role='user' AND empty sender) for legacy rows."""
        if human_sender is not None and str(human_sender).strip():
            row = self._conn.execute(
                """SELECT COUNT(*) FROM messages
                   WHERE (sender = ? OR (role = 'user' AND (sender = '' OR sender IS NULL)))
                     AND timestamp >= ?""",
                (human_sender.strip(), since_ts),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role='user' AND timestamp >= ?",
                (since_ts,),
            ).fetchone()
        return row[0] if row else 0

    @staticmethod
    def read_user_message_timestamps(storage_root: str, since_ts: float) -> List[float]:
        """Read timestamps of human messages (role='user') with timestamp >= since_ts straight
        from an existing chat_records.db, skipping __init__'s side effects (makedirs, schema /
        JSON migration). Used to scan every profile and group cheaply; returns [] if unreadable."""
        db_path = os.path.join(storage_root, "chat_records.db")
        if not os.path.exists(db_path):
            return []
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            try:
                rows = conn.execute(
                    "SELECT timestamp FROM messages WHERE role='user' AND timestamp >= ?",
                    (since_ts,),
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            logger.debug("[ConversationStore] read_user_message_timestamps failed (%s): %s", db_path, e)
            return []
        return [float(r[0]) for r in rows]

    def total_user_messages_count(self, human_sender: Optional[str] = None) -> int:
        """Count all human messages. When human_sender set, includes sender=human_sender OR
        (role='user' AND empty sender) so legacy user messages without sender are counted."""
        if human_sender is not None and str(human_sender).strip():
            row = self._conn.execute(
                """SELECT COUNT(*) FROM messages
                   WHERE sender = ? OR (role = 'user' AND (sender = '' OR sender IS NULL))""",
                (human_sender.strip(),),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role='user'"
            ).fetchone()
        return row[0] if row else 0

    def get_recent_ai_messages(self, n: int) -> List[str]:
        """Completed assistant contents for the legacy text-only emotion reader."""
        rows = self._conn.execute(
            "SELECT content FROM messages WHERE role='assistant' AND generation_status='completed' "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            (n,)
        ).fetchall()
        rows.reverse()
        return [r[0] for r in rows]

    def get_context_start_ts(self, n_turns: int) -> float:
        """Return the timestamp of the oldest message in the effective LLM context window.

        Combines the context_since_ts hard cutoff with the n_turns rolling limit.
        Returns 0.0 if the store is empty.
        """
        since = self._context_since_ts
        row = self._conn.execute("""
            SELECT MIN(timestamp) FROM (
                SELECT timestamp FROM messages
                WHERE role IN ('user', 'assistant')
                  AND (? = 0 OR timestamp >= ?)
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
            )
        """, (since, since, n_turns * 2)).fetchone()
        return row[0] if row and row[0] is not None else 0.0

    @property
    def context_since_ts(self) -> float:
        return self._context_since_ts
