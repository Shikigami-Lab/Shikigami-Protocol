"""群组管理 — groups/<group_id>/ 目录与 group_config.json。

方案 B：群聊会话实体，历史独立于 profile，复用 ConversationStore。
"""
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from src.utils.paths import get_project_root

logger = logging.getLogger(__name__)

_GROUPS_DIR = "groups"
_GROUP_CONFIG_FILE = "group_config.json"


def _groups_root(project_root: str) -> str:
    return os.path.join(project_root, _GROUPS_DIR)


class GroupManager:
    """群组 CRUD 与群会话 store 访问。"""

    def __init__(self, config: Any = None):
        self.config = config
        self._root = get_project_root()
        self._groups_dir = _groups_root(self._root)
        self._store_cache: Dict[str, Any] = {}  # group_id -> ConversationStore

    def _group_path(self, group_id: str) -> str:
        return os.path.join(self._groups_dir, group_id)

    def _config_path(self, group_id: str) -> str:
        return os.path.join(self._group_path(group_id), _GROUP_CONFIG_FILE)

    def list_groups(self) -> List[Dict[str, Any]]:
        """列出所有群组（扫描 groups/ 下含 group_config.json 的子目录）。"""
        result = []
        if not os.path.isdir(self._groups_dir):
            return result
        for name in os.listdir(self._groups_dir):
            path = os.path.join(self._groups_dir, name)
            if not os.path.isdir(path):
                continue
            cfg_path = os.path.join(path, _GROUP_CONFIG_FILE)
            if not os.path.isfile(cfg_path):
                continue
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg.setdefault("group_id", name)
                result.append(cfg)
            except Exception as e:
                logger.warning("[GroupManager] 读取群配置失败 %s: %s", name, e)
        result.sort(key=lambda g: g.get("created_at") or 0, reverse=True)
        return result

    def get_group_ids_for_profile(self, profile_id: str) -> List[str]:
        """返回该 profile 参与的所有群 group_id 列表（单聊合并群近期 / merged_history 用）。"""
        if not (profile_id and profile_id.strip()):
            return []
        pid = profile_id.strip()
        out = []
        for g in self.list_groups():
            gid = g.get("group_id")
            if not gid:
                continue
            for p in g.get("participants") or []:
                if (p.get("profile_id") or "").strip() == pid:
                    out.append(gid)
                    break
        return out

    def get_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        """获取单个群配置，不存在返回 None。"""
        path = self._config_path(group_id)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg.setdefault("group_id", group_id)
            return cfg
        except Exception as e:
            logger.warning("[GroupManager] get_group %s 失败: %s", group_id, e)
            return None

    def create_group(
        self,
        display_name: str,
        participants: List[Dict[str, Any]],
        user_name: str = "",
        orchestrator: str = "random",
        max_replies_per_turn: int = 0,
        system_hint: str = "",
    ) -> Dict[str, Any]:
        """创建群组，写入 group_config.json，返回完整配置（含 group_id）。max_replies_per_turn=0 表示不限制。participants 每项可含 muted: bool。"""
        group_id = uuid.uuid4().hex[:12]
        group_path = self._group_path(group_id)
        os.makedirs(group_path, exist_ok=True)
        if not user_name and self.config:
            user_name = getattr(self.config, "user_name", "用户") or "用户"
        cfg = {
            "group_id": group_id,
            "display_name": (display_name or "未命名群组").strip(),
            "user_name": (user_name or "用户").strip(),
            "participants": list(participants) if participants else [],
            "orchestrator": (orchestrator or "random").strip(),
            "max_replies_per_turn": max(0, int(max_replies_per_turn)) if max_replies_per_turn else 0,
            "system_hint": (system_hint or "").strip(),
            "created_at": time.time(),
        }
        path = self._config_path(group_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info("[GroupManager] 创建群组 id=%s name=%s participants=%d",
                    group_id, cfg["display_name"], len(cfg["participants"]))
        return cfg

    def update_group(self, group_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """部分更新群配置（如 display_name、participants、orchestrator、system_hint）。"""
        cfg = self.get_group(group_id)
        if not cfg:
            return None
        for k in ("display_name", "user_name", "participants", "orchestrator", "max_replies_per_turn", "system_hint"):
            if k in updates:
                cfg[k] = updates[k]
        path = self._config_path(group_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return cfg

    def delete_group(self, group_id: str) -> bool:
        """删除群组（目录及配置）。返回是否删除成功。先关闭该群的 store 以释放 chat_records.db。"""
        group_path = self._group_path(group_id)
        if not os.path.isdir(group_path):
            return False
        store = self._store_cache.pop(group_id, None)
        if store is not None and hasattr(store, "close"):
            try:
                store.close()
            except Exception as e:
                logger.warning("[GroupManager] 关闭群 %s store 时异常: %s", group_id, e)
        try:
            import shutil
            shutil.rmtree(group_path)
            logger.info("[GroupManager] 已删除群组 id=%s", group_id)
            return True
        except Exception as e:
            logger.warning("[GroupManager] delete_group %s 失败: %s", group_id, e)
            return False

    def get_conversation_store(self, group_id: str):
        """获取该群的 ConversationStore（复用接口，storage_root=groups/<group_id>/）。"""
        if group_id in self._store_cache:
            return self._store_cache[group_id]
        cfg = self.get_group(group_id)
        if not cfg:
            return None
        from src.memory.conversation_store import ConversationStore
        store = ConversationStore(self._group_path(group_id), max_turns=0)
        self._store_cache[group_id] = store
        return store
