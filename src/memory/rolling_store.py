"""会话内滚动摘要存储 — profiles/{id}/rolling_summary.json

保存滚动压缩摘要，最多一份。摘要与日期无关：只记录生成时间（updated_at），
不跨天重置、不交接给日摘要；由 MemoryManager 决定何时压缩/压缩多少，本层只管读写。
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
        """返回当前记录 {summary, covered_up_to_id, updated_at}；无则 None。"""
        # Another store instance may have invalidated this file after raw deletion.
        self._load()
        return dict(self._data) if self._data else None

    def update(self, summary: str, covered_up_to_id: int, **progress):
        """写入/覆盖滚动摘要，记录生成时间。"""
        data = {
            **progress,
            "summary": summary.strip(),
            "covered_up_to_id": int(covered_up_to_id),
            "updated_at": time.time(),
        }
        self._save(data)
        self._data = data

    def clear(self) -> bool:
        """清空摘要（如有）。返回是否有内容被清掉。"""
        if self.get() is None:
            return False
        self._save(None)
        self._data = None
        return True

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load(self):
        if not os.path.exists(self._path):
            self._data = None
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict) and (data.get("summary") or data.get("covered_up_to_id")):
                data.pop("date", None)  # 旧格式带 date 字段，忽略
                self._data = data
            else:
                self._data = None
        except Exception as e:
            logger.error("[RollingSummaryStore] 加载失败 %s: %s", self._path, e)
            self._data = None

    def _save(self, data):
        tmp = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            if data is None:
                if os.path.exists(self._path):
                    os.remove(self._path)
                return
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, self._path)
        except Exception as e:
            logger.error("[RollingSummaryStore] 保存失败: %s", e)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise
