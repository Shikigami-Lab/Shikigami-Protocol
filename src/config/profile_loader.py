import json
import logging
import os
from typing import Any, Dict, List

from src.utils.paths import get_resource_path

logger = logging.getLogger(__name__)

# 使用绝对路径确保在打包后能准确定位 profiles 目录
PROFILES_DIR = get_resource_path("profiles")


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
            return json.load(f)
