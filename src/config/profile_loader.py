import json
import logging
import os
from typing import Any, Dict, List

from src.utils.paths import get_resource_path

logger = logging.getLogger(__name__)

# 使用绝对路径确保在打包后能准确定位 profiles 目录
PROFILES_DIR = get_resource_path("profiles")


def _migrate_user_persona(card: Dict[str, Any]) -> bool:
    """惰性迁移老版 user_persona schema → 新版。

    老：{name, description, personality, role_in_story}
    新：{name, introduction}
    返回 True 表示发生了迁移（仅内存修改，不写回文件）。
    """
    up = card.get("user_persona")
    if not isinstance(up, dict):
        return False
    if up.get("introduction"):
        return False
    desc = (up.get("description") or "").strip()
    pers = (up.get("personality") or "").strip()
    role = (up.get("role_in_story") or "").strip()
    if not any([desc, pers, role]):
        return False
    parts = [s for s in [desc, pers, role] if s]
    up["introduction"] = "\n\n".join(parts)
    up.pop("description", None)
    up.pop("personality", None)
    up.pop("role_in_story", None)
    card["user_persona"] = up
    return True


# user_portrait_config 默认值的单一真相源（profile_loader 补全、settings API 回退共用）
DEFAULT_USER_PORTRAIT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "auto_refresh_in_daily_job": True,
    "min_turns_for_burst_refresh": 100,
    "min_hours_between_refresh": 6,
    "use_day_summary_as_input": True,
    "use_facts_as_input": True,
    "max_input_conv_turns": 60,
}


def _ensure_user_portrait_defaults(card: Dict[str, Any]) -> None:
    """确保 user_portrait / user_portrait_config 字段存在（仅内存补全）。"""
    card.setdefault("user_portrait", {
        "content": "",
        "updated_at": 0,
        "refresh_count": 0,
        "last_input_signature": "",
    })
    card.setdefault("user_portrait_config", dict(DEFAULT_USER_PORTRAIT_CONFIG))


class ProfileLoader:
    def list_profiles(self) -> List[Dict[str, Any]]:
        """Return full profile cards for all profile JSON files."""
        result = []
        if not os.path.exists(PROFILES_DIR):
            return result
        for fname in sorted(os.listdir(PROFILES_DIR)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(PROFILES_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    card = json.load(f)
                # Ensure required keys exist with defaults
                card.setdefault("profile_id", fname[:-5])
                card.setdefault("display_name", fname[:-5])
                card.setdefault("base_prompt", "")
                card.setdefault("style_constraint", "")
                card.setdefault("chaos_config", {"enabled": False})
                card.setdefault("load_into_chat", True)  # False = 仅存储不预加载，设置里勾选后才加载
                _migrate_user_persona(card)
                _ensure_user_portrait_defaults(card)
                result.append(card)
            except Exception as e:
                logger.warning(f"[ProfileLoader] skipping {fname}: {e}")
        return result

    def load(self, profile_id: str) -> Dict[str, Any]:
        """Load and return a full profile card by profile_id."""
        path = os.path.join(PROFILES_DIR, f"{profile_id}.json")
        if not os.path.exists(path):
            raise FileNotFoundError(f"Profile not found: {profile_id} (looked at {path})")
        with open(path, "r", encoding="utf-8") as f:
            card = json.load(f)
        _migrate_user_persona(card)
        _ensure_user_portrait_defaults(card)
        return card
