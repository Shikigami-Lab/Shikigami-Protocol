import json
import gc
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from src.memory.conversation_store import ConversationStore

logger = logging.getLogger(__name__)

_RUNTIME_STATE_FILE = "runtime_state.json"


@dataclass
class Session:
    id: str
    profile_id: str
    display_name: str
    storage_root: str
    created_at: float = field(default_factory=time.time)

    # ── Cached ConversationStore (lives for session lifetime) ────────────────
    _store: Any = field(default=None, init=False, repr=False)

    # ── Runtime state (persisted to runtime_state.json) ──────────────────────
    # Reflection engine output: {thought, urgency, topic_anchor, updated_at}
    reflection_state: Optional[Dict[str, Any]] = field(default=None, repr=False)
    # Timestamp of the most recent user message (for silent_seconds calculation)
    last_user_message_time: float = field(default_factory=time.time, repr=False)
    # Base64 screenshot uploaded by frontend for ASE VLM integration
    last_screenshot_b64: Optional[str] = field(default=None, repr=False)
    # Current ASE activity mode (low / medium / high / game / focus)
    ase_mode: str = field(default="medium", repr=False)
    # Segment cooldown: segment_id -> last_fired (epoch seconds), persisted so cooldown survives restart
    segment_last_fired: Dict[str, float] = field(default_factory=dict, repr=False)
    # Segment once_per_day: segment_id -> last fired date "YYYY-MM-DD"
    segment_last_fired_date: Dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_profile(cls, profile_id: str, display_name: str) -> "Session":
        """Create a Session bound 1:1 to a profile (id == profile_id)."""
        return cls(
            id=profile_id,
            profile_id=profile_id,
            display_name=display_name,
            storage_root=f"profiles/{profile_id}",
        )

    # ── ConversationStore accessor ───────────────────────────────────────────

    @property
    def conversation_store(self) -> "ConversationStore":
        """Return the cached ConversationStore for this session (lazy init)."""
        if self._store is None:
            from src.memory.conversation_store import ConversationStore
            self._store = ConversationStore(self.storage_root, max_turns=0)
        return self._store

    # ── Runtime state persistence ─────────────────────────────────────────────

    def get_segment_last_fired(self, segment_id: str) -> float:
        """Return last fired time (epoch seconds) for a segment; 0 if never."""
        return self.segment_last_fired.get(segment_id, 0.0)

    def set_segment_last_fired(self, segment_id: str, ts: float) -> None:
        """Record that a segment just fired; persisted with save_runtime_state()."""
        self.segment_last_fired[segment_id] = ts

    def get_segment_last_fired_date(self, segment_id: str) -> Optional[str]:
        """Return last fired date (YYYY-MM-DD) for once_per_day; None if never."""
        return self.segment_last_fired_date.get(segment_id)

    def set_segment_last_fired_date(self, segment_id: str, date_str: str) -> None:
        """Record that a segment fired today (for once_per_day)."""
        self.segment_last_fired_date[segment_id] = date_str

    def save_runtime_state(self) -> None:
        """Persist volatile runtime state so it survives server restarts."""
        path = os.path.join(self.storage_root, _RUNTIME_STATE_FILE)
        data = {
            "reflection_state": self.reflection_state,
            "ase_mode": self.ase_mode,
            "last_user_message_time": self.last_user_message_time,
            "segment_last_fired": self.segment_last_fired,
            "segment_last_fired_date": self.segment_last_fired_date,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[Session] save_runtime_state failed: %s", e)

    def load_runtime_state(self) -> None:
        """Load persisted runtime state from disk on startup."""
        path = os.path.join(self.storage_root, _RUNTIME_STATE_FILE)
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("reflection_state"):
                self.reflection_state = data["reflection_state"]
            if data.get("ase_mode"):
                self.ase_mode = data["ase_mode"]
            if data.get("last_user_message_time"):
                self.last_user_message_time = float(data["last_user_message_time"])
            if data.get("segment_last_fired") and isinstance(data["segment_last_fired"], dict):
                self.segment_last_fired = {
                    k: float(v) for k, v in data["segment_last_fired"].items()
                }
            if data.get("segment_last_fired_date") and isinstance(data["segment_last_fired_date"], dict):
                self.segment_last_fired_date = dict(data["segment_last_fired_date"])
            logger.debug("[Session] loaded runtime state for profile=%s", self.profile_id)
        except Exception as e:
            logger.warning("[Session] load_runtime_state failed: %s", e)

    def shutdown(self) -> None:
        """Best-effort close cached resources (e.g. SQLite handles).

        This is required when deleting profiles on Windows; otherwise
        `chat_records.db` may remain locked and the directory deletion will fail.
        """
        try:
            store = getattr(self, "_store", None)
            if store is not None:
                close_fn = getattr(store, "close", None)
                if callable(close_fn):
                    close_fn()
        except Exception as e:
            logger.debug("[Session] shutdown failed (ignored): %s", e)
        finally:
            self._store = None
            gc.collect()
