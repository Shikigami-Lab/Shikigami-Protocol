"""中期记忆存储 — profiles/{id}/day_summaries.json

保存按天生成的对话摘要，最多保留 keep_days 天。
事实库永久保存，但摘要可以被 _prune() 老化（只删最旧的摘要）。
"""
import json
import logging
import os
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class DaySummaryStore:

    def __init__(self, storage_root: str):
        self._path = os.path.join(storage_root, "day_summaries.json")
        self._summaries: List[Dict] = []
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def add_summary(self, date_str: str, summary: str, message_count: int = 0,
                    keep_days: int = 14):
        """写入一天的摘要，然后裁剪旧记录。"""
        # 如果已有同一天的，覆盖
        self._summaries = [s for s in self._summaries if s.get("date") != date_str]
        self._summaries.append({
            "date": date_str,
            "summary": summary.strip(),
            "message_count": message_count,
            "generated_at": time.time(),
        })
        self._summaries.sort(key=lambda s: s.get("date", ""))
        self._prune(keep_days)
        self._save()

    def get_recent(self, n: int = 3) -> List[Dict]:
        """返回最近 n 天的摘要（日期升序）。"""
        return self._summaries[-n:] if len(self._summaries) > n else list(self._summaries)

    def has_summary_for(self, date_str: str) -> bool:
        return any(s.get("date") == date_str for s in self._summaries)

    def delete(self, date_str: str) -> bool:
        before = len(self._summaries)
        self._summaries = [s for s in self._summaries if s.get("date") != date_str]
        if len(self._summaries) < before:
            self._save()
            return True
        return False

    def clear_all(self) -> int:
        """清空全部摘要，返回删除条数。"""
        n = len(self._summaries)
        if n == 0:
            return 0
        self._summaries = []
        self._save()
        return n

    def count(self) -> int:
        return len(self._summaries)

    def get_all(self) -> List[Dict]:
        return list(self._summaries)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _prune(self, keep_days: int = 14):
        """删除超出天数的旧摘要（只削减摘要，不动事实库）。"""
        if len(self._summaries) > keep_days:
            self._summaries = self._summaries[-keep_days:]

    def _load(self):
        if not os.path.exists(self._path):
            self._summaries = []
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            self._summaries = data.get("summaries", [])
        except Exception as e:
            logger.error("[DaySummaryStore] 加载失败 %s: %s", self._path, e)
            self._summaries = []

    def _save(self):
        tmp = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump({"summaries": self._summaries}, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.error("[DaySummaryStore] 保存失败: %s", e)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
