"""Build and validate evidence-backed fact changes for a single extraction batch."""

from datetime import date, datetime, time, timedelta
import math
import re
import time as clock
import unicodedata
import uuid

from src.memory.long_term_store import EXPERIENCE_CONTENT_LIMIT, LongTermFact, _VALID_CATEGORIES
from src.utils.memory_time import has_floating_date

FORMAT = """Return a JSON array of memory operations. An empty array means nothing worth remembering.
Use add for a genuinely new durable fact, confirm for an already known fact, update for an
explicit correction of the same fact or continuation of the same experience, and skip for
small talk, speculation or one-off details.
Use kind stable for durable facts, address only for the user's current preferred way to
address this AI, event for a scheduled or historical event, temporary for time-limited context,
and experience for a meaningful past episode the user actually described, whether shared
with the character or personally recounted. Preserve who was present; do not turn a story
someone told the character into an event the character witnessed.
For event set event_date (YYYY-MM-DD) and for temporary set expires_on (YYYY-MM-DD, inclusive).
Resolve relative dates for events and temporary context against the cited message times.
A scheduled date passing does not prove it happened.
For experience, write up to 1200 characters and explain why it matters in retention_reason.
Keep distinct experiences separate. Use the same candidate ID to continue a specific experience,
even if it is cold; similarity alone is insufficient. Set occurred_from and/or occurred_to
(YYYY-MM-DD) only when the date appears explicitly in cited user speech. Omit uncertain dates;
never use message timestamps as occurrence dates. Keep existing dates during continuation.
For confirm and update, copy fact_id and record_version (as expected_version) from the candidate.
Include event_date when confirming an event. When rescheduling, update the same event only
when the cited message explicitly gives both the old and new dates.
For add and update include content, category (habit, preference, taboo, relationship,
location, milestone, ai_insight or other) and weight (0-2). Optional fields: keywords
(short strings) and emotional_note (the character's brief reaction). Each change must include
evidence, an array of exact reference strings (including any slice suffix), with at least one
from the user's own statements. For experience only, you may also cite a completed assistant
message to show what the assistant said. User assertions require user speech. Quoted text,
plans, roleplay, and assistant guesses do not establish that something happened.
Example: [{"op":"add","kind":"stable","category":"preference","content":"用户喝茶不加糖",
"weight":1.2,"evidence":["single:42"]}]. Follow this operation format even if earlier instructions describe an older format.
"""

_KINDS = {"stable", "address", "event", "temporary", "experience"}
_CJK = re.compile(r"[\u3400-\u9fff]+")
_WORD = re.compile(r"[a-z0-9]+")
_ISO_DATE = re.compile(r"(?<![0-9])\d{4}-\d{2}-\d{2}(?![0-9])")
EXTRACTION_CANDIDATE_LIMIT = 16
_RRF_K = 60
_STOP = {
    "用户", "喜欢", "之前", "今天", "现在", "一下", "那个", "那次", "起来", "什么", "记得", "自己", "我们", "user",
    "a", "an", "the", "this", "that", "these", "those",
    "i", "me", "my", "mine", "we", "us", "our", "ours", "you", "your", "yours",
    "he", "him", "his", "she", "her", "hers", "it", "its", "they", "them", "their", "theirs",
    "am", "is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "have", "has", "had",
    "can", "could", "may", "might", "must", "shall", "should", "will", "would",
    "and", "or", "but", "if", "as", "because", "than", "not",
    "about", "after", "at", "before", "by", "for", "from", "in", "into", "of", "on", "to", "with", "without",
    "what", "when", "where", "which", "who", "whom", "whose", "why", "how",
    # The tokenizer splits English contractions at the apostrophe.
    "d", "ll", "m", "re", "s", "t", "ve",
}


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().strip().rstrip("。.!！").split())


def _terms(text):
    text = normalize(text)
    cjk = {part[i:i + 2] for part in _CJK.findall(text) for i in range(len(part) - 1)}
    return (cjk | set(_WORD.findall(text))) - _STOP


def rank_related(facts, query, limit):
    terms = _terms(query)
    scores = []
    for fact in facts:
        overlap = terms & _terms(" ".join((fact.content, *(str(tag) for tag in fact.tags or ()))))
        if overlap:
            scores.append((len(overlap), fact.weight, fact.updated_at, fact))
    scores.sort(key=lambda row: row[:3], reverse=True)
    return [row[-1] for row in scores[:limit]]


def extraction_candidate_query(rows):
    return "\n".join(row["content"] for row in rows if row["role"] == "user")


def choose_candidates(facts, rows, *, vector_rows=(), vector_matches=None, max_distance=0.45):
    """Rank writable snapshot facts. Existing callers use the lexical path."""
    limit = EXTRACTION_CANDIDATE_LIMIT
    now = clock.time()
    query = extraction_candidate_query(rows)
    dates = set(_ISO_DATE.findall(query))
    eligible = {
        fact.id: fact for fact in sorted(facts, key=lambda fact: fact.id)
        if _writable_target(fact, now)
        and (not dates or not fact.event_date or fact.event_date in dates)
    }
    address = next((fact for fact in eligible.values() if fact.kind == "address"), None)
    ranked_pool = {key: fact for key, fact in eligible.items() if fact.kind != "address"}
    lexical = rank_related(ranked_pool.values(), query, limit)
    semantic = _rank_vector_candidates(ranked_pool, vector_rows, vector_matches, max_distance, limit)
    direct = []
    if semantic:
        direct = _direct_source_candidates(ranked_pool.values(), rows)[:limit]
        scores = {}
        for ranking in ([fact.id for fact in lexical], semantic):
            for position, fact_id in enumerate(ranking, start=1):
                scores[fact_id] = scores.get(fact_id, 0.0) + 1 / (_RRF_K + position)
        ranked = [ranked_pool[fact_id] for fact_id in sorted(scores, key=lambda fid: (-scores[fid], fid))]
    else:
        ranked = lexical
    ordered = ([address] if address else []) + direct + ranked
    return list({fact.id: fact for fact in ordered}.values())[:limit]


def _rank_vector_candidates(facts, rows, matches, max_distance, limit):
    if matches is None or not isinstance(rows, (list, tuple)):
        return []
    best = {}
    for row in rows:
        metadata = row.get("metadata") if isinstance(row, dict) else None
        fact_id = metadata.get("fact_id") if isinstance(metadata, dict) else None
        fact = facts.get(fact_id) if isinstance(fact_id, str) else None
        distance = row.get("distance") if fact is not None else None
        if (type(distance) in (int, float) and 0 <= distance < max_distance
                and matches(fact, row)):
            best[fact_id] = min(distance, best.get(fact_id, math.inf))
    return sorted(best, key=lambda fid: (best[fid], fid))[:limit]


def _direct_source_key(row):
    source, message_id, timestamp = row.get("source"), row.get("id"), row.get("timestamp")
    if (not isinstance(source, str) or not source or type(message_id) is not int or message_id <= 0
            or type(timestamp) not in (int, float) or not 0 < timestamp < math.inf):
        return None
    start, end = row.get("start_char"), row.get("end_char")
    if ("start_char" in row or "end_char" in row) and (
        type(start) is not int or type(end) is not int or not 0 <= start < end
    ):
        return None
    return source, message_id, start, end, timestamp


def _direct_source_candidates(facts, rows):
    sources = {}
    for row in rows:
        key = _direct_source_key(row) if row.get("role") == "user" else None
        if key is None:
            continue
        source, message_id, start, end, _ = key
        ref = f"{source}:{message_id}" + (f"#{start}-{end}" if start is not None else "")
        digest = row.get("content_hash")
        if row.get("reference", ref) == ref and (
            "content_hash" not in row or (isinstance(digest, str) and digest)
        ):
            sources[key] = digest
    result = []
    for fact in facts:
        for ref in fact.evidence:
            if (not isinstance(ref, dict) or ref.get("speaker_role", ref.get("role")) != "user"
                    or ref.get("assertion", "self_statement") != "self_statement"):
                continue
            key = _direct_source_key(ref)
            digest = ref.get("content_hash")
            if key in sources and ("content_hash" not in ref or
                                   (isinstance(digest, str) and digest and sources[key] == digest)):
                result.append(fact)
                break
    return result


def _is_cold_experience(fact):
    return fact.kind == "experience" and fact.archived and fact.archive_reason == "cold_memory"


def _parse_date(value):
    if not isinstance(value, str):
        return None
    try:
        result = date.fromisoformat(value)
    except ValueError:
        return None
    return result if result.isoformat() == value else None


def _supported_evidence(operation, rows_by_ref, *, experience):
    refs = operation.get("evidence")
    if not isinstance(refs, list) or not refs or len(refs) > len(rows_by_ref):
        raise ValueError("Operation must cite supplied messages")
    evidence = []
    for ref in refs:
        row = rows_by_ref.get(ref) if isinstance(ref, str) else None
        if not row:
            raise ValueError("Operation cites an unknown message")
        role = row.get("role")
        if role != "user" and not (
            experience and role == "assistant" and row.get("generation_status", "completed") == "completed"
        ):
            raise ValueError("Unsupported dialogue evidence role or generation status")
        source, message_id = row.get("source"), row.get("id")
        if not isinstance(source, str) or not source or type(message_id) is not int or message_id <= 0:
            raise ValueError("Invalid evidence source or message ID")
        expected_ref = f"{source}:{message_id}"
        item = {"source": source, "id": message_id, "timestamp": row["timestamp"],
                "speaker_role": role, "assertion": "self_statement" if role == "user" else "character_context"}
        if "start_char" in row or "end_char" in row:
            start, end = row.get("start_char"), row.get("end_char")
            if type(start) is not int or type(end) is not int or not 0 <= start < end:
                raise ValueError("Invalid evidence character range")
            expected_ref += f"#{start}-{end}"
            item.update(start_char=start, end_char=end)
        if row.get("reference", expected_ref) != expected_ref or ref != expected_ref:
            raise ValueError("Evidence reference does not match its source row")
        if "content_hash" in row:
            content_hash = row["content_hash"]
            if not isinstance(content_hash, str) or not content_hash:
                raise ValueError("Invalid evidence content hash")
            item["content_hash"] = content_hash
        evidence.append(item)
    if not any(item["speaker_role"] == "user" for item in evidence):
        raise ValueError("Operation must cite messages spoken by the user")
    return evidence


def _user_dates(operation, rows_by_ref):
    return {
        value for ref in operation["evidence"] if rows_by_ref[ref]["role"] == "user"
        for value in _ISO_DATE.findall(rows_by_ref[ref]["content"])
    }


def _experience_dates(operation, target, dates, evidence):
    old_start = target.occurred_from if target else None
    old_end = target.occurred_to if target else None
    bounds = []
    for key, old in (("occurred_from", old_start), ("occurred_to", old_end)):
        proposed = operation.get(key)
        if proposed is not None and (
            not _parse_date(proposed) or (old and proposed != old) or (proposed != old and proposed not in dates)
        ):
            raise ValueError("Experience dates must be explicit in user evidence and cannot replace known dates")
        if operation["op"] == "confirm" and proposed is not None and proposed != old:
            raise ValueError("Use update to add a newly evidenced experience date")
        bounds.append(proposed if proposed is not None else old)
    start, end = bounds
    if start and end and start > end:
        raise ValueError("Experience start date follows its end date")
    if start or end:
        try:
            latest = max(datetime.fromtimestamp(item["timestamp"]).date().isoformat()
                         for item in evidence if item["speaker_role"] == "user")
        except (TypeError, OSError, OverflowError, ValueError) as error:
            raise ValueError("Experience dates require a valid source timestamp") from error
        if max(start or "", end or "") > latest:
            raise ValueError("A future date is a plan, not a completed experience")
    if dates and (old_start or old_end) and not any(
        (start or end) <= cited <= (end or start) for cited in dates
    ):
        raise ValueError("Cited dates do not match the target experience")
    return start, end


def _experience_metadata(operation, target, dates, content, evidence):
    if not 4 <= len(content) <= EXPERIENCE_CONTENT_LIMIT or has_floating_date(content):
        raise ValueError("Invalid experience length or unresolved relative date")
    start, end = _experience_dates(operation, target, dates, evidence)
    if set(_ISO_DATE.findall(content)) - (dates | {start, end}):
        raise ValueError("Experience text contains dates absent from the user evidence")
    reason = operation.get("retention_reason", target.retention_reason if target else "")
    if not isinstance(reason, str) or (not target and not reason.strip()):
        raise ValueError("A new experience requires a retention reason")
    return {"occurred_from": start, "occurred_to": end, "retention_reason": reason.strip()}


def _evidence_key(row):
    return row["source"], row["id"], row.get("start_char"), row.get("end_char")


def _same_experience(fact, metadata, evidence):
    dates = metadata["occurred_from"], metadata["occurred_to"]
    if fact.kind != "experience" or dates != (fact.occurred_from, fact.occurred_to):
        return False
    # Even identical dated text can describe separate interactions.
    return bool(
        {_evidence_key(item) for item in evidence if item["speaker_role"] == "user"}
        & {_evidence_key(item) for item in fact.evidence if item.get("speaker_role", "user") == "user"}
    )


def _protected(fact):
    return fact.is_manual or fact.source == "manual" or fact.pinned or fact.curation_protected


def _writable_target(fact, now):
    return (not _protected(fact) and (fact.expires_at is None or fact.expires_at > now)
            and (fact.is_active(now) or _is_cold_experience(fact)))


def apply_operations(facts, operations, rows_by_ref, candidates, threshold):
    """Mutate only a detached snapshot. Return IDs whose indexed text changed."""
    changed = set()
    by_id = {fact.id: fact for fact in facts}
    allowed = {fact.id: fact.record_version for fact in candidates}
    now = clock.time()
    for operation in operations[:24]:
        if not isinstance(operation, dict):
            continue
        action = operation.get("op")
        if action not in ("add", "confirm", "update"):
            continue
        target = None
        if action != "add":
            target_id = operation.get("fact_id")
            if not isinstance(target_id, str) or target_id not in allowed or target_id not in by_id:
                raise ValueError("Operation must target a supplied candidate")
            target = by_id[target_id]
        target_kind = (target.kind or ("event" if target.event_date else "stable")) if target else "stable"
        kind = operation.get("kind", target_kind)
        experience = kind == "experience"
        if target and experience != (target.kind == "experience"):
            raise ValueError("Cannot change a record into or out of an experience")
        evidence = _supported_evidence(operation, rows_by_ref, experience=experience)
        if target:
            # Older callers may omit the token. They still target the supplied snapshot version.
            expected = operation.get("expected_version", allowed[target.id])
            if type(expected) is not int or expected != allowed[target.id] or expected != target.record_version:
                raise ValueError("Operation targets a stale memory version")
            if not _writable_target(target, now) or (target.event_date and kind != "event"):
                continue
        if action == "confirm":
            if experience:
                _experience_dates(operation, target, _user_dates(operation, rows_by_ref), evidence)
            if target.event_date:
                dates = _user_dates(operation, rows_by_ref)
                if operation.get("event_date") != target.event_date or (dates and target.event_date not in dates):
                    continue
            _merge_evidence(target, evidence)
            continue

        content = operation.get("content")
        if not isinstance(content, str) or not isinstance(kind, str) or kind not in _KINDS:
            if experience:
                raise ValueError("Experience requires text")
            continue
        content = content.strip()
        metadata = {}
        if experience:
            metadata = _experience_metadata(operation, target, _user_dates(operation, rows_by_ref), content, evidence)
        elif not 4 <= len(content) <= 160 or has_floating_date(content):
            continue
        category = operation.get("category", target.category if target else "other")
        if not isinstance(category, str) or category not in _VALID_CATEGORIES:
            continue
        if kind == "address":
            category = "preference"
        try:
            weight = float(operation.get("weight", target.weight if target else 1.0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(weight) or not threshold < weight <= 2:
            continue
        event = _parse_date(operation.get("event_date")) if kind == "event" else None
        expiry = _parse_date(operation.get("expires_on")) if kind == "temporary" else event
        if kind in ("event", "temporary") and (not expiry or expiry == date.max):
            continue
        dates = set()
        if event:
            dates = _user_dates(operation, rows_by_ref)
            if dates and event.isoformat() not in dates:
                continue
        try:
            expires_at = datetime.combine(expiry + timedelta(days=1), time.min).timestamp() if expiry else None
        except (OverflowError, OSError, ValueError):
            continue
        event_date = event.isoformat() if event else None
        tags = operation.get("keywords")
        if isinstance(tags, str):
            tags = tags.split()
        tags = ([item[:48] for item in tags[:12] if isinstance(item, str)] if isinstance(tags, list)
                else target.tags if experience and target else [])
        if target:
            if target.kind not in ("", kind) or target.category != category:
                continue
            if event and target.event_date != event_date and (
                not target.event_date or not {target.event_date, event_date} <= dates
            ):
                continue
            if kind == "temporary" and target.kind != kind:
                continue
            if (normalize(target.content) == normalize(content) and target.expires_at == expires_at
                    and (not experience or target.tags == tags)
                    and all(getattr(target, key) == value for key, value in metadata.items())):
                _merge_evidence(target, evidence)
                continue
        duplicate = next((fact for fact in facts if fact is not target
                          and (not fact.archived or _is_cold_experience(fact))
                          and fact.event_date == event_date and fact.expires_at == expires_at
                          and ((experience and _same_experience(fact, metadata, evidence))
                               or (not experience and fact.kind != "experience"))
                          and normalize(fact.content) == normalize(content)), None)
        if duplicate:
            if experience and target:
                raise ValueError("Experience update conflicts with another record")
            if _writable_target(duplicate, now) and not duplicate.archived:
                _merge_evidence(duplicate, evidence)
            continue
        note = operation.get("emotional_note", target.emotional_note if experience and target else "")
        note = note.strip()[:15] if isinstance(note, str) else ""
        index_changed = target is None or target.content != content or target.tags != tags
        if not target:
            if kind == "address" and any(f.kind == "address" and f.is_active() for f in facts):
                continue
            target = LongTermFact(content=content, source="auto", is_manual=False, category=category,
                                  tags=tags, kind=kind, weight=round(weight, 2), emotional_note=note,
                                  event_date=event_date, expires_at=expires_at, **metadata)
            facts.append(target)
        else:
            target.versions.append(target.snapshot())
            if target.archived:
                target.archive_revision = uuid.uuid4().hex
            target.content = content
            target.updated_at = datetime.now().timestamp()
            if not experience:
                target.evidence = []
            for key, value in metadata.items():
                setattr(target, key, value)
            target.kind = kind
            target.event_date = event_date
            target.expires_at = expires_at
            target.weight = round(weight, 2)
            target.tags = tags
            target.emotional_note = note
        _merge_evidence(target, evidence)
        if index_changed or not experience:
            changed.add(target.id)
    return changed


def _merge_evidence(fact, evidence):
    unique = {_evidence_key(row): row for row in [*fact.evidence, *evidence]}
    ordered = list(unique.values())
    fact.evidence = ordered if len(ordered) <= 16 else [ordered[0], *ordered[-15:]]
