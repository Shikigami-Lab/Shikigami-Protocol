"""会话内滚动摘要存储 — profiles/{id}/rolling_summary.json

保存"当天早些时候"的滚动压缩摘要，最多一份（只覆盖当天）。
跨天时由 MemoryManager 决定交接给日摘要或直接重置；本层只管读写。
"""
import json
import logging
import os
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class RollingSummaryStore:

    def __init__(self, storage_root: str):
        self._path = os.path.join(storage_root, "rolling_summary.json")
        self._data: Optional[Dict] = None
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self) -> Optional[Dict]:
        """返回当前记录 {date, summary, covered_up_to_id, updated_at}；无则 None。"""
        return dict(self._data) if self._data else None

    def update(self, summary: str, covered_up_to_id: int, date_str: str):
        """写入/覆盖当天的滚动摘要。"""
        self._data = {
            "date": date_str,
            "summary": summary.strip(),
            "covered_up_to_id": int(covered_up_to_id),
            "updated_at": time.time(),
        }
        self._save()

    def reset(self, date_str: str):
        """跨天重置：清空摘要，covered 归零（日摘要已关闭或无需交接时）。"""
        if self._data is None:
            return
        self._data = None
        self._save()

    def clear_if_date(self, date_str: str) -> bool:
        """日摘要交接成功后调用：若记录属于该日期则清除。"""
        if self._data and self._data.get("date") == date_str:
            self._data = None
            self._save()
            return True
        return False

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load(self):
        if not os.path.exists(self._path):
            self._data = None
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict) and data.get("date") and data.get("summary"):
                self._data = data
            else:
                self._data = None
        except Exception as e:
            logger.error("[RollingSummaryStore] 加载失败 %s: %s", self._path, e)
            self._data = None

    def _save(self):
        tmp = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            if self._data is None:
                if os.path.exists(self._path):
                    os.remove(self._path)
                return
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._data, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.error("[RollingSummaryStore] 保存失败: %s", e)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
