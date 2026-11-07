"""Shared admission rules for query-driven long-term memory recall."""

import hashlib
import json
import logging
import math
import time
from datetime import datetime

from src.memory.long_term_store import LongTermFact, LongTermStore

logger = logging.getLogger(__name__)


def semantic_version(fact: LongTermFact) -> str:
    """Fingerprint the fields that determine whether a merge still represents the same fact."""
    payload = {
        "content": fact.content,
        "category": fact.category,
        "kind": fact.kind,
        "event_date": fact.event_date,
        "expires_at": fact.expires_at,
        **({"occurred_from": fact.occurred_from, "occurred_to": fact.occurred_to} if fact.kind == "experience" else {}),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def experience_links_by_day(facts):
    """Index dialogue dates for experience links, separately from when an event happened."""
    result = {}
    for fact in facts:
        if fact.kind != "experience":
            continue
        for reference in fact.evidence:
            value = reference.get("timestamp") if isinstance(reference, dict) else None
            if type(value) not in (int, float) or not math.isfinite(value):
                continue
            try:
                day = datetime.fromtimestamp(value).date().isoformat()
            except (OSError, OverflowError, ValueError):
                continue
            result.setdefault(day, set()).add(fact.id)
    return result


def is_cold(fact: LongTermFact, now: float) -> bool:
    return (fact.archived and fact.archive_reason == "cold_memory"
            and (fact.expires_at is None or fact.expires_at > now))


class RecallCandidates:
    """Read authoritative records after retrieval before resolving ranked hits."""

    def __init__(self, store: LongTermStore):
        self.now = time.time()
        active_ids = {fact.id for fact in store.get_recallable(self.now)}
        self.facts = store.get_all()
        self.active = {fact.id: fact for fact in self.facts
                       if fact.id in active_ids and fact.is_active(self.now)}
        self.cold = {fact.id: fact for fact in self.facts if is_cold(fact, self.now)}
        self.indexable = {**self.active, **self.cold}
        self.aliases = {}
        for fact in self.facts:
            target_id = fact.merged_into
            if (fact.archived and fact.archive_reason == "merged"
                    and isinstance(target_id, str) and target_id in self.active
                    and (fact.expires_at is None or fact.expires_at > self.now)
                    and fact.merged_source_version and fact.merged_target_version):
                target = self.active[target_id]
                if (fact.merged_source_version == semantic_version(fact)
                        and fact.merged_target_version == semantic_version(target)):
                    self.aliases[fact.id] = target

    def ranked_sources(self, excluded=()) -> dict[str, LongTermFact]:
        """Search alias text, but resolve hits to eligible current representatives."""
        return {
            fact.id: fact for fact in self.facts
            if (fact.id in self.active or fact.id in self.cold or fact.id in self.aliases)
            and self.resolve(fact.id).id not in excluded
        }

    def resolve(self, fact_id: str) -> LongTermFact | None:
        return self.aliases.get(fact_id) or self.active.get(fact_id) or self.cold.get(fact_id)

    def vector_hits(self, sources, rows, matches, max_distance, *, cold_distance=None):
        """Yield validated fact hits for automatic recall."""
        seen = set()
        for row in rows:
            hit = self._fact_vector_hit(sources, row, matches, max_distance, cold_distance)
            if hit and hit[0].id not in seen:
                fact, distance = hit
                seen.add(fact.id)
                yield fact, distance

    def _fact_vector_hit(self, sources, row, matches, max_distance, cold_distance):
        metadata = row.get("metadata")
        fact_id = metadata.get("fact_id") if isinstance(metadata, dict) else None
        source = self.indexable.get(fact_id) if isinstance(fact_id, str) and fact_id in sources else None
        if source is None:
            return None
        threshold = cold_distance if cold_distance is not None and fact_id in self.cold else max_distance
        distance = _vector_distance(row, threshold)
        if distance is not None and matches(source, row):
            return source, distance
        return None


def _vector_distance(row, max_distance):
    distance = row.get("distance", 1.0)
    if isinstance(distance, (int, float)) and math.isfinite(distance) and distance < max_distance:
        return distance
    return None


def search_vectors(vectors, query, limit):
    try:
        # Leave room for obsolete rows in the one embedding query.
        return vectors.search(query, limit=min(limit * 3, 60))
    except Exception as error:
        logger.debug("Fact vector search failed: %s", error)
        return []
