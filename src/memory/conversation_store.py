import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Message:
    role: str       # "user" | "assistant" | "system"
    content: str
    timestamp: float = 0.0
    sender: str = ""  # 发言者标识：用户名或人格 display_name，群聊防污染用

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

        # Schema migration: add sender column if missing (existing DBs)
        try:
            cur = conn.execute("PRAGMA table_info(messages)")
            columns = [row[1] for row in cur.fetchall()]
            if "sender" not in columns:
                conn.execute("ALTER TABLE messages ADD COLUMN sender TEXT NOT NULL DEFAULT ''")
                conn.commit()
                logger.info("[ConversationStore] migrated schema: added sender column (%s)", self._db_path)
        except Exception as e:
            logger.warning("[ConversationStore] sender column migration failed: %s", e)

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

    def append(self, role: str, content: str, sender: str = ""):
        ts = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO messages (role, content, timestamp, sender) VALUES (?, ?, ?, ?)",
                (role, content, ts, (sender or "").strip())
            )
        if self.max_turns > 0:
            limit = self.max_turns * 2
            with self._conn:
                self._conn.execute("""
                    DELETE FROM messages WHERE id NOT IN (
                        SELECT id FROM messages ORDER BY timestamp DESC LIMIT ?
                    )
                """, (limit,))

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
        """Replace all stored messages with the given list (used for message deletion)."""
        with self._conn:
            self._conn.execute("DELETE FROM messages")
            self._conn.executemany(
                "INSERT INTO messages (role, content, timestamp, sender) VALUES (?, ?, ?, ?)",
                [(m.role, m.content, m.timestamp, getattr(m, "sender", "") or "") for m in messages]
            )

    # ── Read operations ──────────────────────────────────────────────────────

    def get_recent(self, n_turns: int = 20) -> List[Dict[str, str]]:
        """Return the last n_turns rounds as [{role, content, sender?}, ...] for the LLM.

        Excludes messages with timestamp < context_since_ts so that a short-term
        memory reset is respected without deleting visible chat history.
        """
        since = self._context_since_ts
        rows = self._conn.execute("""
            SELECT role, content, sender FROM messages
            WHERE role IN ('user', 'assistant')
              AND (? = 0 OR timestamp >= ?)
            ORDER BY timestamp DESC
            LIMIT ?
        """, (since, since, n_turns * 2)).fetchall()
        rows.reverse()
        return [{"role": r[0], "content": r[1], "sender": (r[2] or "") if len(r) > 2 else ""} for r in rows]

    def get_recent_with_ts(self, n_turns: int = 20) -> List[Dict]:
        """Like get_recent but each dict includes 'timestamp' for merging with group messages."""
        since = self._context_since_ts
        rows = self._conn.execute("""
            SELECT role, content, sender, timestamp FROM messages
            WHERE role IN ('user', 'assistant')
              AND (? = 0 OR timestamp >= ?)
            ORDER BY timestamp DESC
            LIMIT ?
        """, (since, since, n_turns * 2)).fetchall()
        rows.reverse()
        return [
            {"role": r[0], "content": r[1], "sender": (r[2] or "") if len(r) > 2 else "", "timestamp": r[3] if len(r) > 3 else 0.0}
            for r in rows
        ]

    def get_recent_by_sender(self, n: int = 10, sender: str = "") -> List[Dict[str, str]]:
        """Return the last n messages where sender matches (e.g. 群聊按发言人召回)."""
        if not (sender and sender.strip()):
            return []
        s = sender.strip()
        rows = self._conn.execute("""
            SELECT role, content, sender FROM messages
            WHERE sender = ? AND role IN ('user', 'assistant')
            ORDER BY timestamp DESC LIMIT ?
        """, (s, n)).fetchall()
        rows.reverse()
        return [{"role": r[0], "content": r[1], "sender": (r[2] or "") if len(r) > 2 else ""} for r in rows]

    def get_recent_since(self, since_ts: float, limit: int = 50) -> List[Dict[str, str]]:
        """Return up to `limit` messages with timestamp >= since_ts (all senders), chronological order.
        Used for 单聊合并群近期：merged_history 在短期上下文时间窗内取群消息。
        """
        items = self.get_recent_since_with_ts(since_ts, limit)
        return [{"role": m["role"], "content": m["content"], "sender": m.get("sender", "")} for m in items]

    def get_recent_since_with_ts(self, since_ts: float, limit: int = 50) -> List[Dict]:
        """Like get_recent_since but each dict includes 'timestamp' for merging (单聊+群聊合并)."""
        if limit <= 0:
            return []
        rows = self._conn.execute("""
            SELECT role, content, sender, timestamp FROM messages
            WHERE role IN ('user', 'assistant') AND timestamp >= ?
            ORDER BY timestamp DESC LIMIT ?
        """, (max(0.0, since_ts), max(1, limit))).fetchall()
        rows.reverse()
        return [
            {"role": r[0], "content": r[1], "sender": (r[2] or "") if len(r) > 2 else "", "timestamp": r[3] if len(r) > 3 else 0.0}
            for r in rows
        ]

    def get_all(self) -> List[Message]:
        """Return all stored messages (for history API / client sync)."""
        rows = self._conn.execute(
            "SELECT role, content, timestamp, sender FROM messages ORDER BY timestamp"
        ).fetchall()
        return [
            Message(role=r[0], content=r[1], timestamp=r[2], sender=(r[3] or "") if len(r) > 3 else "")
            for r in rows
        ]

    def get_all_with_id(self) -> List[Dict]:
        """Return all messages as dicts with id, role, content, timestamp, sender (for paginated history)."""
        rows = self._conn.execute(
            "SELECT id, role, content, timestamp, sender FROM messages ORDER BY timestamp"
        ).fetchall()
        return [
            {"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3], "sender": (r[4] or "") if len(r) > 4 else ""}
            for r in rows
        ]

    def search_messages(self, q: str = "", limit: int = 50,
                        offset: int = 0) -> List[Dict]:
        """Search messages by content (LIKE), newest first, paginated.

        Returns list of dicts with keys: id, role, content, timestamp.
        Empty q returns all messages (newest first).
        """
        pattern = f"%{q}%" if q else "%"
        rows = self._conn.execute("""
            SELECT id, role, content, timestamp, sender FROM messages
            WHERE content LIKE ?
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """, (pattern, limit, offset)).fetchall()
        return [
            {"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3], "sender": (r[4] or "") if len(r) > 4 else ""}
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
                   ORDER BY timestamp DESC LIMIT 1""",
                (human_sender.strip(),),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT timestamp FROM messages WHERE role='user' ORDER BY timestamp DESC LIMIT 1"
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
        """返回最近 n 条 assistant 消息的 content（用于情绪分类器）。"""
        rows = self._conn.execute(
            "SELECT content FROM messages WHERE role='assistant' ORDER BY timestamp DESC LIMIT ?",
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
                ORDER BY timestamp DESC
                LIMIT ?
            )
        """, (since, since, n_turns * 2)).fetchone()
        return row[0] if row and row[0] is not None else 0.0

    @property
    def context_since_ts(self) -> float:
        return self._context_since_ts
