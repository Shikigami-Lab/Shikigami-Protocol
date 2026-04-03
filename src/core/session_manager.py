import json
import logging
import os
import shutil
from typing import Dict, List, Optional

from src.core.session import Session
from src.utils.paths import get_project_root, get_resource_path

logger = logging.getLogger(__name__)

# 使用统一的路径工具，支持打包后的路径定位
_META_PATH = get_resource_path(os.path.join("config", "sessions_meta.json"))


class SessionManager:
    def __init__(self, config):
        self.config = config
        self._sessions: Dict[str, Session] = {}
        self._current_id: Optional[str] = None

    async def init(self):
        """Load sessions from profiles directory (1:1 profile-session binding)."""
        from src.config.profile_loader import ProfileLoader

        current_id_hint = None

        self._profile_order: List[str] = []

        if os.path.exists(_META_PATH):
            try:
                with open(_META_PATH, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                if "sessions" in meta:
                    # Old format: run one-time migration, then remap current_id
                    self._migrate_sessions(meta)
                    old_current = meta.get("current_id")
                    for s in meta.get("sessions", []):
                        if s["id"] == old_current:
                            current_id_hint = s["profile_id"]
                            break
                else:
                    current_id_hint = meta.get("current_id")
                self._profile_order = meta.get("profile_order") or []
            except Exception as e:
                logger.warning(f"[SessionManager] failed to load sessions_meta: {e}")

        # Build session list from profiles directory — only load those with load_into_chat True
        profiles = ProfileLoader().list_profiles()
        all_profile_ids = [p["profile_id"] for p in profiles]
        # Keep full profile_order (all ids) so sidebar order is stable; list_sessions() returns only loaded
        self._profile_order = [pid for pid in self._profile_order if pid in all_profile_ids]
        for pid in all_profile_ids:
            if pid not in self._profile_order:
                self._profile_order.append(pid)

        for p in profiles:
            if not p.get("load_into_chat", True):
                continue
            session = Session.from_profile(p["profile_id"], p["display_name"])
            session.load_runtime_state()
            if not session.reflection_state:
                try:
                    user_name = getattr(self.config, "user_name", "用户") or "用户"
                    ts = session.conversation_store.last_user_message_time(user_name)
                    if ts:
                        session.last_user_message_time = ts
                except Exception as e:
                    logger.warning("[SessionManager] could not rebuild last_user_message_time for %s: %s",
                                   session.id, e)
            self._sessions[session.id] = session

        if not self._sessions:
            logger.warning("[SessionManager] no profiles found — waiting for profile creation")
            self._current_id = None
            self._save_meta()
            return

        # Resolve current session
        if current_id_hint and current_id_hint in self._sessions:
            self._current_id = current_id_hint
        elif self._current_id not in self._sessions:
            self._current_id = next(iter(self._sessions), None)

        self._save_meta()
        logger.info(
            f"[SessionManager] loaded {len(self._sessions)} profile-session(s), "
            f"current: {self._current_id}"
        )

    def _migrate_sessions(self, old_meta: dict):
        """One-time migration: copy session chat_records to profiles/<id>/."""
        for old_session in old_meta.get("sessions", []):
            profile_id = old_session.get("profile_id", "")
            if not profile_id:
                continue
            dst_dir = os.path.join(get_project_root(), "profiles", profile_id)
            dst = os.path.join(dst_dir, "chat_records.json")
            os.makedirs(dst_dir, exist_ok=True)

            # Prefer session chat_records over short_memory
            src = os.path.join("sessions", old_session["id"], "chat_records.json")
            short_mem = os.path.join(dst_dir, "short_memory.json")

            if os.path.exists(src) and not os.path.exists(dst):
                shutil.copy(src, dst)
                logger.info(f"[SessionManager] migrated {src} → {dst}")
            elif os.path.exists(short_mem) and not os.path.exists(dst):
                shutil.copy(short_mem, dst)
                logger.info(f"[SessionManager] migrated {short_mem} → {dst}")

        logger.info("[SessionManager] one-time session migration complete")

    def get_current(self) -> Optional[Session]:
        if self._current_id:
            return self._sessions.get(self._current_id)
        return None

    def get_by_id(self, session_id: str) -> Optional[Session]:
        """Return session by id; if not loaded (e.g. unchecked personality in group), load on demand."""
        if session_id in self._sessions:
            return self._sessions[session_id]
        from src.config.profile_loader import ProfileLoader
        try:
            card = ProfileLoader().load(session_id)
            session = Session.from_profile(session_id, card.get("display_name", session_id))
            session.load_runtime_state()
            # Only register in _sessions (visible in sidebar) if load_into_chat is True.
            # Profiles with load_into_chat=False are returned as ephemeral sessions for
            # group-chat/background use without appearing in the sidebar.
            if card.get("load_into_chat", True):
                self._sessions[session_id] = session
                if session_id not in self._profile_order:
                    self._profile_order.append(session_id)
            logger.info("[SessionManager] on-demand loaded session=%s (persistent=%s)",
                        session_id, card.get("load_into_chat", True))
            return session
        except Exception as e:
            logger.warning("[SessionManager] get_by_id(%s) failed: %s", session_id, e)
            return None

    def list_sessions(self) -> List[Session]:
        ordered = [self._sessions[pid] for pid in self._profile_order if pid in self._sessions]
        for sid, s in self._sessions.items():
            if s not in ordered:
                ordered.append(s)
        return ordered

    def switch(self, session_id: str) -> Session:
        if session_id not in self._sessions:
            raise ValueError(f"Session '{session_id}' not found")
        self._current_id = session_id
        self._save_meta()
        logger.info(f"[SessionManager] switched to: {self._sessions[session_id].display_name}")
        return self._sessions[session_id]

    def add_session(self, profile_id: str, display_name: str) -> Session:
        """Register a session for a newly created profile."""
        session = Session.from_profile(profile_id, display_name)
        self._sessions[session.id] = session
        if profile_id not in self._profile_order:
            self._profile_order.append(profile_id)
        self._save_meta()
        return session

    def unload_session(self, profile_id: str) -> None:
        """Unload a session from memory (e.g. user unchecked 加载到聊天). Keeps profile in _profile_order."""
        s = self._sessions.get(profile_id)
        if s and hasattr(s, "shutdown"):
            try:
                s.shutdown()
            except Exception:
                pass
        self._sessions.pop(profile_id, None)
        if self._current_id == profile_id:
            self._current_id = next(iter(self._sessions), None)
        self._save_meta()
        logger.info("[SessionManager] unloaded session=%s (still in profile_order)", profile_id)

    def load_session(self, profile_id: str) -> Optional[Session]:
        """Load a single profile into _sessions (e.g. user checked 加载到聊天). Returns the session or None."""
        if profile_id in self._sessions:
            return self._sessions[profile_id]
        from src.config.profile_loader import ProfileLoader
        profiles = ProfileLoader().list_profiles()
        p = next((x for x in profiles if x["profile_id"] == profile_id), None)
        if not p or not p.get("load_into_chat", True):
            return None
        session = Session.from_profile(p["profile_id"], p.get("display_name", profile_id))
        session.load_runtime_state()
        if not session.reflection_state:
            try:
                user_name = getattr(self.config, "user_name", "用户") or "用户"
                ts = session.conversation_store.last_user_message_time(user_name)
                if ts:
                    session.last_user_message_time = ts
            except Exception as e:
                logger.warning("[SessionManager] could not rebuild last_user_message_time for %s: %s",
                               profile_id, e)
        self._sessions[session.id] = session
        if profile_id not in self._profile_order:
            self._profile_order.append(profile_id)
        self._save_meta()
        logger.info("[SessionManager] loaded session=%s into memory", profile_id)
        return session

    def remove_session(self, session_id: str):
        """Remove a session when its profile is deleted."""
        s = self._sessions.get(session_id)
        if s and hasattr(s, "shutdown"):
            try:
                s.shutdown()
            except Exception:
                pass
        self._sessions.pop(session_id, None)
        if session_id in self._profile_order:
            self._profile_order.remove(session_id)
        if self._current_id == session_id:
            self._current_id = next(iter(self._sessions), None)
        self._save_meta()

    def create_session(self, profile_id: str, display_name: str) -> Session:
        """Legacy compatibility alias for add_session."""
        return self.add_session(profile_id, display_name)

    def set_profile_order(self, profile_ids: List[str]) -> None:
        """Set the display order of profiles/sessions. Persisted to sessions_meta. Keeps all profile ids."""
        from src.config.profile_loader import ProfileLoader
        all_ids = [p["profile_id"] for p in ProfileLoader().list_profiles()]
        self._profile_order = [pid for pid in profile_ids if pid in all_ids]
        for pid in all_ids:
            if pid not in self._profile_order:
                self._profile_order.append(pid)
        self._save_meta()

    def _save_meta(self):
        os.makedirs(os.path.dirname(_META_PATH), exist_ok=True)
        data = {"current_id": self._current_id, "profile_order": self._profile_order}
        with open(_META_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def cleanup(self):
        pass
