"""ChromaDB index for the current facts of one profile."""
import logging
import os
import time
import gc
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class VectorMemoryStore:

    def __init__(self, storage_root: str, embed_config: dict, *, existing_only: bool = False):
        self._chroma_path = os.path.join(storage_root, "chroma_db")
        self._embed_config = embed_config
        self._client = None
        self._collection = None
        self._embed_fn = None
        self._available = False
        self._actual_provider_name: str = embed_config.get("provider", "none")
        if existing_only:
            self._open_existing_index()
        else:
            self._init()

    def _open_existing_index(self):
        """Open an old index for migration and cleanup without loading an embedding model."""
        if not os.path.isfile(os.path.join(self._chroma_path, "chroma.sqlite3")):
            return
        try:
            import chromadb  # type: ignore

            self._client = chromadb.PersistentClient(path=self._chroma_path)
            self._collection = self._client.get_collection(name="memories", embedding_function=None)
            self._available = True
        except Exception as error:
            logger.warning("[VectorStore] Existing index unavailable: %s", error)

    def _init(self):
        try:
            import chromadb  # type: ignore
            from src.memory.embedding import make_chroma_embedding_function

            embed_fn = make_chroma_embedding_function(self._embed_config)
            if embed_fn is None:
                logger.info("[VectorStore] embedding 不可用，向量层已禁用")
                return

            self._actual_provider_name = getattr(embed_fn, "_actual_provider",
                                                  self._embed_config.get("provider", "none"))
            os.makedirs(self._chroma_path, exist_ok=True)
            self._client = chromadb.PersistentClient(path=self._chroma_path)
            self._embed_fn = embed_fn
            self._collection = self._client.get_or_create_collection(
                name="memories",
                embedding_function=embed_fn,
                metadata={"hnsw:space": "cosine"},
            )
            self._available = True
            logger.info("[VectorStore] ChromaDB 已初始化 path=%s count=%d",
                        self._chroma_path, self._collection.count())
        except ImportError:
            logger.info("[VectorStore] chromadb 未安装，向量层已禁用")
        except Exception as e:
            logger.error("[VectorStore] 初始化失败: %s", e)

    def is_available(self) -> bool:
        return self._available

    def upsert_fact(self, text: str, metadata: Dict) -> Optional[str]:
        """Index one fact under its stable ID. An embedding failure keeps the old row."""
        if not self._available or not text.strip():
            return None
        fact_id = metadata["fact_id"]
        try:
            self._collection.upsert(documents=[text], metadatas=[metadata], ids=[fact_id])
            self.delete_by_fact_ids([fact_id], keep_id=fact_id)
            return fact_id
        except Exception as error:
            logger.error("[VectorStore] upsert failed for %s: %s", fact_id, error)
            return None

    def link_fact(self, vector_id: str, fact_id: str) -> bool:
        """Associate a legacy row with its saved fact without regenerating the embedding."""
        try:
            self._collection.update(ids=[vector_id], metadatas=[{"fact_id": fact_id}])
            return True
        except Exception as error:
            logger.warning("[VectorStore] legacy link failed for %s: %s", vector_id, error)
            return False

    def search(self, query: str, limit: int = 5) -> List[Dict]:
        """语义检索，返回 [{id, document, distance}]。"""
        if not self._available or not query.strip():
            return []
        try:
            n = min(limit, max(1, self._collection.count()))
            results = self._collection.query(
                query_texts=[query],
                n_results=n,
                include=["documents", "distances", "metadatas"],
            )
            items = []
            ids = results.get("ids", [[]])[0]
            docs = results.get("documents", [[]])[0]
            dists = results.get("distances", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            for i, mid in enumerate(ids):
                items.append({
                    "id": mid,
                    "document": docs[i] if i < len(docs) else "",
                    "distance": dists[i] if i < len(dists) else 1.0,
                    "metadata": metas[i] if i < len(metas) else {},
                })
            return items
        except Exception as e:
            logger.error("[VectorStore] search 失败: %s", e)
            return []

    def get_all(self, *, strict: bool = False) -> List[Dict]:
        """返回全部记忆条目 [{id, document, metadata}]。"""
        if not self._available:
            return []
        try:
            results = self._collection.get(include=["documents", "metadatas"])
            items = []
            for i, mid in enumerate(results.get("ids", [])):
                items.append({
                    "id": mid,
                    "document": results["documents"][i] if i < len(results["documents"]) else "",
                    "metadata": results["metadatas"][i] if i < len(results["metadatas"]) else {},
                })
            return items
        except Exception as e:
            logger.error("[VectorStore] get_all 失败: %s", e)
            if strict:
                raise
            return []

    def delete(self, memory_id: str) -> bool:
        if not self._available:
            return False
        try:
            self._collection.delete(ids=[memory_id])
            return True
        except Exception as e:
            logger.error("[VectorStore] delete 失败: %s", e)
            return False

    def delete_by_fact_ids(self, fact_ids: List[str], *, keep_id: Optional[str] = None) -> int:
        """删除 metadata.fact_id 在 fact_ids 中的向量（用于孤儿清理、合并时删旧）。返回删除条数。"""
        if not self._available or not fact_ids:
            return 0
        try:
            rows = self._collection.get(where={"fact_id": {"$in": fact_ids}}, include=[])
            to_delete = [vector_id for vector_id in rows["ids"] if vector_id != keep_id]
            if to_delete:
                self._collection.delete(ids=to_delete)
                logger.info("[VectorStore] delete_by_fact_ids 删除 %d 条 fact_ids=%s", len(to_delete), fact_ids[:5])
            return len(to_delete)
        except Exception as e:
            logger.error("[VectorStore] delete_by_fact_ids 失败: %s", e)
            return 0

    def get_all_fact_ids(self) -> set:
        """返回向量库中所有条目 metadata.fact_id 的集合（用于增量同步检测）。"""
        return {
            r["metadata"].get("fact_id")
            for r in self.get_all()
            if r.get("metadata", {}).get("fact_id")
        }

    def count_orphans(self, valid_fact_ids: Set[str]) -> int:
        """统计孤儿向量数量（仅统计不删除，用于预览）。"""
        if not self._available:
            return 0
        valid = set(valid_fact_ids)
        try:
            all_items = self.get_all()
            return sum(
                1 for r in all_items
                if r.get("metadata", {}).get("fact_id") and r["metadata"].get("fact_id") not in valid
            )
        except Exception as e:
            logger.error("[VectorStore] count_orphans 失败: %s", e)
            return 0

    def delete_orphans(self, valid_fact_ids: Set[str]) -> int:
        """删除 metadata.fact_id 不在 valid_fact_ids 中的向量（孤儿向量清理）。返回删除条数。"""
        if not self._available:
            return 0
        valid = set(valid_fact_ids)
        try:
            all_items = self.get_all()
            to_delete = [
                r["id"] for r in all_items
                if r.get("metadata", {}).get("fact_id") and r["metadata"].get("fact_id") not in valid
            ]
            if to_delete:
                self._collection.delete(ids=to_delete)
                logger.info("[VectorStore] delete_orphans 删除 %d 条", len(to_delete))
            return len(to_delete)
        except Exception as e:
            logger.error("[VectorStore] delete_orphans 失败: %s", e)
            return 0

    def count(self) -> int:
        if not self._available:
            return 0
        try:
            return self._collection.count()
        except Exception:
            return 0

    def clear_all(self) -> int:
        """清空向量库中所有条目，返回删除数量。"""
        if not self._available:
            return 0
        try:
            n = self._collection.count()
            all_ids = [r["id"] for r in self.get_all()]
            if all_ids:
                self._collection.delete(ids=all_ids)
            logger.info("[VectorStore] 清空全部向量 (%d 条)", n)
            return n
        except Exception as e:
            logger.error("[VectorStore] clear_all 失败: %s", e)
            return 0

    def close(self) -> None:
        """Best-effort close/release resources for safe filesystem deletion.

        ChromaDB PersistentClient 在某些平台（尤其 Windows）可能会保持文件句柄，
        需要在删除 profile 目录前尽力释放引用并触发 GC。
        """
        try:
            if self._client is not None:
                # 先删除 collection，触发 ChromaDB 释放内部写锁
                try:
                    self._client.delete_collection("memories")
                except Exception:
                    pass
                # 部分版本提供 reset()，可关闭内部 SQLite 连接
                reset_fn = getattr(self._client, "reset", None)
                if callable(reset_fn):
                    try:
                        reset_fn()
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("[VectorStore] close failed (ignored): %s", e)
        finally:
            self._collection = None
            self._client = None
            self._embed_fn = None
            self._available = False
            gc.collect()
