"""memory_changelog.py —— 每日记忆/遗忘任务的变更日志 + 回滚。

每次每日任务若实际改动了 ≥1 条事实，写一条记录到 profiles/<id>/memory_changelog.json
（保留最近 MAX_MEMORY_CHANGELOG 条，仿 persona_evolution / user_portrait changelog）。

记录里 summary 供 UI 显示、rollback_data 供回滚。回滚逐条独立：
  - 权重：对当前权重应用逆因子（old/new），与其他次的权重变化可交换
  - 合并新增的摘要事实：从事实库 + 向量库删除
  - 合并移除的旧事实向量：事实仍在档案里，重新嵌入向量库
  - 孤儿向量清理：不可回滚（对应事实已被手动删除），仅记数
"""
import json
import logging
import os
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_CHANGELOG_FILE = "memory_changelog.json"
MAX_MEMORY_CHANGELOG = 20


def _path(storage_root: str) -> str:
    return os.path.join(storage_root, _CHANGELOG_FILE)


def get_changelog(storage_root: str) -> List[Dict[str, Any]]:
    """返回变更日志列表（最新在前）。损坏/缺失返回 []。"""
    p = _path(storage_root)
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("[memory_changelog] 读取失败 %s: %s", p, e)
        return []


def _save_changelog(storage_root: str, entries: List[Dict[str, Any]]) -> None:
    """原子写入（tmp + replace）。"""
    p = _path(storage_root)
    tmp = p + ".tmp"
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception as e:
        logger.warning("[memory_changelog] 写入失败 %s: %s", p, e)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def record_run(storage_root: str, entry: Dict[str, Any]) -> None:
    """追加一条 run 记录，裁剪到最近 MAX 条。best-effort，异常不抛出。"""
    try:
        entries = get_changelog(storage_root)
        entries.insert(0, entry)
        _save_changelog(storage_root, entries[:MAX_MEMORY_CHANGELOG])
        logger.info("[memory_changelog] 记录变更 run_id=%s", entry.get("run_id"))
    except Exception as e:
        logger.warning("[memory_changelog] record_run 失败: %s", e)


def has_meaningful_changes(rollback_data: Dict[str, Any]) -> bool:
    """判断本次运行是否实际改动了事实（决定是否写日志，空跑不记）。"""
    if rollback_data.get("decay") or rollback_data.get("reinforce"):
        return True
    cons = rollback_data.get("consolidation") or {}
    return bool(cons.get("added") or cons.get("vectors_removed"))


async def rollback(storage_root: str, run_id: str, mgr) -> Dict[str, Any]:
    """回滚指定 run。逐条独立（见模块 docstring）。返回 {ok, restored, skipped, ...}。"""
    log = get_changelog(storage_root)
    entry = next((e for e in log if e.get("run_id") == run_id), None)
    if entry is None:
        return {"ok": False, "error": "run_not_found"}
    if entry.get("rolled_back"):
        return {"ok": False, "error": "already_rolled_back"}

    rb = entry.get("rollback_data") or {}
    restored = 0
    skipped = 0

    # ── 权重：逆因子作用于当前权重 ────────────────────────────────────────────
    for item in list(rb.get("decay") or []) + list(rb.get("reinforce") or []):
        fid = item.get("id")
        old = item.get("old")
        new = item.get("new")
        fact = mgr.facts.get_by_id(fid) if fid else None
        if not fact or not new:
            skipped += 1
            continue
        try:
            mgr.facts.update(fid, weight=round(fact.weight * (float(old) / float(new)), 2))
            restored += 1
        except Exception as e:
            logger.debug("[memory_changelog] 权重回滚跳过 %s: %s", fid, e)
            skipped += 1

    cons = rb.get("consolidation") or {}

    # ── 合并新增的摘要事实：事实库 + 向量库都删 ───────────────────────────────
    for fid in cons.get("added") or []:
        try:
            if mgr.facts.get_by_id(fid):
                mgr.facts.delete(fid)
                if mgr.vectors and mgr.vectors.is_available():
                    mgr.vectors.delete_by_fact_ids([fid])
                restored += 1
            else:
                skipped += 1
        except Exception as e:
            logger.debug("[memory_changelog] 摘要删除跳过 %s: %s", fid, e)
            skipped += 1

    # ── 合并移除的旧事实向量：事实仍在档案里，重新嵌入 ────────────────────────
    for fid in cons.get("vectors_removed") or []:
        try:
            if await mgr.reembed_fact(fid):
                restored += 1
            else:
                skipped += 1
        except Exception as e:
            logger.debug("[memory_changelog] 向量重嵌跳过 %s: %s", fid, e)
            skipped += 1

    entry["rolled_back"] = True
    entry["rolled_back_at"] = int(time.time())
    _save_changelog(storage_root, log)
    logger.info("[memory_changelog] 回滚完成 run_id=%s restored=%d skipped=%d",
                run_id, restored, skipped)
    return {"ok": True, "restored": restored, "skipped": skipped}
