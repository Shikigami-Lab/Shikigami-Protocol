"""BM25 关键词检索器 — 混合检索的关键词分支

可选依赖 rank_bm25；未安装时 is_available() 返回 False，search() 返回空列表。

分词策略（无需 jieba）：
  - 英文/数字：按空格切分
  - 中文字符：逐字切分
  - 混合词（如 "iPhone用户"）：字母部分保留整体，汉字逐字拆开

用法（调用方负责传入候选列表，不缓存索引）：
  bm25 = BM25Retriever()
  results = bm25.search(candidate_facts, query, limit=20)
  # -> [(fact_id, score), ...] 按 score 降序，score=0 已过滤
"""
import logging
import re
from typing import List, Tuple

logger = logging.getLogger(__name__)


def _tokenize(text: str) -> List[str]:
    """中英混合简易分词：英文按空格，中文按单字符。"""
    tokens: List[str] = []
    for word in text.lower().split():
        if re.search(r"[\u4e00-\u9fff]", word):
            # 含汉字：逐字符拆分（ASCII 数字/字母一并拆开，避免搜不到）
            tokens.extend(list(word))
        else:
            tokens.append(word)
    return tokens


class BM25Retriever:
    """基于 rank_bm25 的无状态关键词检索器。

    每次调用 search() 都会现场建立 BM25Okapi 索引，适合候选集 < 5000 条的场景
    （886 条约 1~3 ms，开销可接受）。
    """

    def __init__(self):
        self._available = False
        try:
            import rank_bm25  # noqa: F401
            self._available = True
            logger.info("[BM25] rank_bm25 可用，关键词混合检索已启用")
        except ImportError:
            logger.info(
                "[BM25] rank_bm25 未安装，关键词检索已禁用"
                "（pip install rank-bm25 可启用）"
            )

    def is_available(self) -> bool:
        return self._available

    def search(self, facts: list, query: str, limit: int = 20) -> List[Tuple[str, float]]:
        """在 facts 上运行 BM25 检索。

        Args:
            facts:  List[LongTermFact] — 候选事实列表（仅搜索此子集）
            query:  检索查询文本
            limit:  返回结果上限（score=0 的条目已过滤）

        Returns:
            [(fact_id, bm25_score), ...] 按 score 降序排列。
        """
        if not self._available or not facts or not query.strip():
            return []
        try:
            from rank_bm25 import BM25Okapi

            corpus = [_tokenize(f.content) for f in facts]
            fact_ids = [f.id for f in facts]
            bm25 = BM25Okapi(corpus)

            query_tokens = _tokenize(query)
            scores = bm25.get_scores(query_tokens)

            ranked = sorted(
                ((fact_ids[i], float(scores[i])) for i in range(len(scores))),
                key=lambda x: x[1],
                reverse=True,
            )
            return [(fid, s) for fid, s in ranked if s > 0.0][:limit]

        except Exception as e:
            logger.warning("[BM25] search 失败: %s", e)
            return []
