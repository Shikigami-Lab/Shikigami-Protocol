"""Choose one same-profile fact to compare during a fixed memory review."""

from collections.abc import Iterable
import math

from src.memory.curation_plan import _dated_fact, _durable, _has_user_evidence, _protected
from src.memory.fact_operations import _terms, normalize, rank_related
from src.memory.long_term_store import LongTermFact
from src.memory.memory_manager import MemoryManager
from src.memory.recall import _rrf_merge
from src.memory.vector_store import VectorMemoryStore


_MAX_COSINE_DISTANCE = 0.20


def _eligible(fact):
    return (isinstance(fact.content, str) and bool(fact.content.strip())
            and _durable(fact) and not _protected(fact) and not _dated_fact(fact))


def _representative_key(fact):
    # Preserve attached user evidence, then direct every comparison toward a smaller key.
    return not _has_user_evidence(fact), fact.id


def _lexical_ranking(subject, candidates):
    terms = _terms(subject.content)
    related = []
    for fact in candidates:
        other = _terms(fact.content)
        overlap = len(terms & other)
        # Require two shared body terms and at least half of the combined terms.
        if overlap >= 2 and 2 * overlap >= len(terms | other):
            related.append(fact)
    return [fact.id for fact in rank_related(related, subject.content, len(related))]


def _stored_unit_vectors(vectors, facts, shape=None):
    rows = vectors.get_fact_embeddings(list(facts))
    if not rows:
        return {}

    import numpy as np  # Chroma dependency, needed only for stored vectors.

    # Prefer the stable row ID over a legacy or staged row with the same fact_id.
    rows.sort(key=lambda row: (row["id"] != row["metadata"].get("fact_id"), row["id"]))
    embeddings = {}
    for row in rows:
        fact_id = row["metadata"].get("fact_id")
        fact = facts.get(fact_id)
        if fact is None or fact_id in embeddings or not MemoryManager.fact_vector_matches(fact, row):
            continue
        try:
            embedding = np.asarray(row["embedding"], dtype=float)
        except (TypeError, ValueError, OverflowError):
            continue
        if embedding.ndim != 1 or (shape is not None and embedding.shape != shape):
            continue
        norm = float(np.linalg.norm(embedding))
        if 0 < norm < math.inf:
            embeddings[fact_id] = embedding / norm
    return embeddings


def _vector_ranking(subject, candidates, vectors):
    if vectors is None:
        return []
    query = _stored_unit_vectors(vectors, {subject.id: subject}).get(subject.id)
    if query is None:
        return []

    hits = []
    for fact_id, embedding in _stored_unit_vectors(vectors, candidates, query.shape).items():
        distance = 1 - float(query @ embedding)
        if distance < _MAX_COSINE_DISTANCE:
            hits.append((distance, fact_id))
    return [fact_id for _, fact_id in sorted(hits)]


def find_review_comparison(
    subject: LongTermFact,
    facts: Iterable[LongTermFact],
    vectors: VectorMemoryStore | None = None,
) -> LongTermFact | None:
    """Find an eligible comparison in the caller's full same-profile snapshot."""
    if not _eligible(subject):
        return None
    text = normalize(subject.content)
    if not text:
        return None

    subject_key = _representative_key(subject)
    candidates = {
        fact.id: fact for fact in sorted(facts, key=lambda fact: fact.id)
        if fact.id != subject.id and fact.category == subject.category
        and _eligible(fact) and _representative_key(fact) < subject_key
    }
    if not candidates:
        return None

    exact = (fact for fact in candidates.values() if normalize(fact.content) == text)
    representative = min(exact, key=_representative_key, default=None)
    if representative is not None:
        return representative

    lexical = _lexical_ranking(subject, candidates.values())
    semantic = _vector_ranking(subject, candidates, vectors)
    ranked = _rrf_merge(lexical, semantic)
    return candidates[ranked[0]] if ranked else None
