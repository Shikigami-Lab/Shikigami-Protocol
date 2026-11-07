"""Preview and apply reversible changes to a profile's existing memories."""

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import re
import time
import uuid

from src.memory.fact_operations import apply_operations, normalize, rank_related
from src.memory.long_term_store import LongTermFact, LongTermStore, fact_action_revision
from src.memory.recall import semantic_version
from src.utils.memory_time import has_floating_date

_BATCH_SIZE = 24
_ARCHIVE_REASONS = {"cold_memory", "low_value", "unsupported"}
_STABLE_CATEGORIES = {"habit", "preference", "taboo", "location"}
_POLICY_VERSION = 3
# The entire note must describe a self-contained, momentary action. Arbitrary
# subjects, objects and trailing clauses could carry information worth keeping.
_ONE_OFF_ACTION = re.compile(
    r"(?:(?:i|the (?:character|assistant)) )?"
    r"(?:waved once|nodded once|said good (?:night|morning) once|"
    r"briefly (?:waved|nodded|wagged my tail))"
    r"|(?:我|角色)?(?:挥了挥手|挥了一下手|点了点头|点了一下头|摇了摇尾巴|坐下揉(?:了揉)?尾巴)"
)
_CORRECTION_OR_BOUNDARY = re.compile(
    r"纠正|更正|澄清|指正|平时|现实中|长期(?:约定|界限)|明确.{0,8}(?:界限|边界)"
    r"|(?:其实|实际上|一直)[^。！？]{0,24}(?:外貌|发色|瞳色|眼镜|耳朵|尾巴)"
    r"|\b(?:corrected|clarified|correction|in real life|everyday appearance|standing boundary)\b"
    r"|\b(?:actually|always|usually|normally)\b[^.!?]{0,40}\b(?:appearance|hair|eyes|glasses|ears|tail)\b"
    r"|\b(?:glasses|hair|eyes)\b[^.!?]{0,24}\b(?:every day|in daily life)\b"
)
Decide = Callable[[list[LongTermFact]], Awaitable[list[dict]]]
_CURATION_COUNTS = ("merged", "archived", "reviewed", "trimmed")
_DAILY_COUNTS = ("requests", "dialogue_messages", "dialogue_slices", "added", "updated", "confirmed", *_CURATION_COUNTS)
_daily_request_start: ContextVar[Callable[[], None] | None] = ContextVar("daily_request_start", default=None)


@dataclass
class DailyDialogue:
    rows: list[dict]
    candidates: list[LongTermFact]
    next_cursors: dict[str, int]
    next_offsets: dict[str, dict]
    is_current: Callable[[], bool]
    threshold: float


@dataclass
class _BulkPage:
    token: str
    profile_id: str
    revision: int
    versions: dict[str, str]
    items: dict[str, dict]
    fixed: bool = False


@dataclass
class _PendingPreview:
    id: str
    versions: dict[str, str]
    items: list[dict]
    promoted: bool


class CurationConflict(Exception):
    """The preview or archived revision is no longer current."""


def _version(fact: LongTermFact) -> str:
    data = fact.to_dict()
    # A review note is an outcome, not new evidence that needs another review.
    data.pop("review_note")
    payload = json.dumps([_POLICY_VERSION, data], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _protected(fact: LongTermFact) -> bool:
    return fact.is_manual or fact.source == "manual" or fact.pinned or fact.curation_protected


def _curation_keep_reason(fact: LongTermFact) -> str | None:
    if _protected(fact):
        return "人工保护 / Protected by a user decision"
    if fact.archived:
        return "已归档 / Already archived"
    if not fact.is_active():
        return "已过期 / Expired"
    if fact.kind == "address":
        return "保留当前称呼 / Preserve current address"
    return None


def _is_durable(fact: LongTermFact) -> bool:
    return (
        not fact.event_date and fact.expires_at is None
        and (fact.kind == "stable" or (not fact.kind and fact.category in _STABLE_CATEGORIES))
    )


def _may_merge(source: LongTermFact, target: LongTermFact) -> bool:
    # Old records lack sufficiently reliable event identities. Keep those for review.
    return (
        _is_durable(source) and _is_durable(target)
        and source.category == target.category
        and all(
            fact.occurred_from is None and fact.occurred_to is None and not has_floating_date(fact.content)
            for fact in (source, target)
        )
    )


def _candidates(facts: list[LongTermFact], reviewed: dict) -> list[LongTermFact]:
    eligible = [fact for fact in facts if _curation_keep_reason(fact) is None]
    pending = [fact for fact in eligible if reviewed.get(fact.id) != _version(fact)]
    if not pending:
        return []
    pending.sort(key=lambda fact: (fact.updated_at, fact.id))
    seed = pending[0]
    related = rank_related([fact for fact in eligible if fact.id != seed.id], seed.content, _BATCH_SIZE // 2)
    grouped_ids = {fact.id for fact in related}
    return [seed, *related, *(fact for fact in pending[1:] if fact.id not in grouped_ids)][:_BATCH_SIZE]


def _valid_evidence_reference(ref) -> bool:
    if not isinstance(ref, dict):
        return False
    source, message_id, timestamp = ref.get("source"), ref.get("id"), ref.get("timestamp")
    start, end = ref.get("start_char"), ref.get("end_char")
    return (
        isinstance(source, str)
        and (source == "single" or (source.startswith("group:") and bool(source[6:].strip())))
        and type(message_id) is int and message_id > 0
        and type(timestamp) in (int, float) and 0 < timestamp < float("inf")
        and (ref.get("speaker_role"), ref.get("assertion")) in (
            ("user", "self_statement"), ("assistant", "character_context"),
        )
        and ref.get("generation_status", "completed") == "completed"
        and ("start_char" not in ref and "end_char" not in ref
             or type(start) is int and type(end) is int and 0 <= start < end)
        and ("content_hash" not in ref
             or isinstance(ref["content_hash"], str) and bool(ref["content_hash"].strip()))
    )


def _valid_experience_dates(start, end) -> bool:
    known = []
    for value in (start, end):
        if value is None:
            continue
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError):
            return False
        if parsed.isoformat() != value or parsed > date.today():
            return False
        known.append(parsed)
    return known == sorted(known)


def _archive_review_reason(fact: LongTermFact, archive_reason: str) -> str | None:
    if fact.event_date or fact.expires_at is not None or fact.kind in ("address", "event", "temporary"):
        return "保留称呼、安排或限时记忆 / Preserve current address, schedule, or time-limited memory"
    if has_floating_date(fact.content):
        return "相对日期需核对原话 / Check the original source for relative dates"
    if _CORRECTION_OR_BOUNDARY.search(normalize(fact.content)):
        return "可能包含长期适用的更正或界限 / Possible durable correction or boundary"
    if _is_durable(fact) or (
        fact.kind != "experience"
        and (fact.category == "relationship" or (fact.category == "milestone" and archive_reason != "cold_memory"))
    ):
        return "可能包含长期有效的信息 / Possible durable information"

    start, end = fact.occurred_from, fact.occurred_to
    if fact.kind == "experience":
        if archive_reason != "cold_memory":
            return "经历需核对归档用途 / Review how the experience should be archived"
        if not _valid_experience_dates(start, end):
            return "经历时间范围无效 / Invalid experience date range"
        if (
            not all(_valid_evidence_reference(ref) for ref in fact.evidence)
            or not any(ref["speaker_role"] == "user" for ref in fact.evidence)
        ):
            return "经历需有有效原话依据 / Experience requires valid source references"
        return None
    if start is not None or end is not None:
        return "发生时间需核对条目类型 / Review the record kind and occurrence dates"
    if archive_reason == "cold_memory":
        return "冷归档需有明确的经历类型 / Cold archive requires a structured experience"
    if not (
        archive_reason == "low_value"
        and not fact.kind and fact.category in ("other", "ai_insight")
        and not (fact.evidence or fact.versions or fact.tags or fact.emotional_note)
        and not fact.retention_reason
        and _ONE_OFF_ACTION.fullmatch(normalize(fact.content))
    ):
        return "缺少可自动归档的明确依据 / No affirmative basis for automatic archive"
    return None


def _item(fact: LongTermFact, action: str, reason: str, *, archive_reason=None, target=None) -> dict:
    return {
        "fact_id": fact.id, "content": fact.content, "action": action, "reason": reason,
        "archive_reason": archive_reason if action == "archive" else None,
        "target_id": target.id if target else None,
        "target_content": target.content if target else None,
    }


def _validate(rows: list[LongTermFact], raw: list[dict]) -> list[dict]:
    by_id = {fact.id: fact for fact in rows}
    decisions = {}
    if not isinstance(raw, list) or len(raw) != len(rows):
        raise ValueError("Incomplete memory curation response")
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Invalid memory curation response")
        fact_id = item.get("fact_id")
        action = item.get("action")
        reason = item.get("reason")
        if (
            not isinstance(fact_id, str) or fact_id not in by_id or fact_id in decisions
            or action not in ("keep", "merge", "archive", "review")
            or not isinstance(reason, str) or not reason.strip()
        ):
            raise ValueError("Invalid memory curation response")
        decisions[fact_id] = item

    items = []
    for fact in rows:
        decision = decisions[fact.id]
        action = decision["action"]
        reason = decision["reason"].strip()[:400]
        archive_reason = decision.get("archive_reason")
        target = None
        review_reason = None
        if action == "archive":
            if archive_reason not in tuple(_ARCHIVE_REASONS):
                raise ValueError("Invalid memory archive reason")
            review_reason = _archive_review_reason(fact, archive_reason)
        elif action == "merge":
            target_id = decision.get("target_id")
            if (
                not isinstance(target_id, str) or target_id == fact.id or target_id not in by_id
                or decisions[target_id]["action"] != "keep"
            ):
                raise ValueError("Invalid memory merge target")
            target = by_id[target_id]
            if not _may_merge(fact, target):
                review_reason = "缺少确认是同一事实的依据 / Evidence needed to establish equivalence"
            elif normalize(fact.content) != normalize(target.content):
                if _CORRECTION_OR_BOUNDARY.search(normalize(fact.content)):
                    review_reason = "可能包含长期适用的更正或界限 / Possible durable correction or boundary"
                else:
                    review_reason = "合并需正文一致 / Merge requires identical normalized text"
        if review_reason:
            action = "review"
            reason = f"{review_reason}: {reason}"
            target = None
        items.append(_item(fact, action, reason, archive_reason=archive_reason, target=target))
    return items


def _fixed_rows(subject, facts):
    """Find an exact representative in the full snapshot before any pagination."""
    normalized = normalize(subject.content)
    matches = [candidate for candidate in facts if candidate.id != subject.id
               and candidate.id < subject.id and _curation_keep_reason(candidate) is None
               and normalize(candidate.content) == normalized and _may_merge(subject, candidate)]
    return [subject, min(matches, key=lambda fact: fact.id)] if matches else [subject]


def _pending_fixed_subject(facts, state):
    reviewed = state.get("reviewed", {})
    return min((fact for fact in facts if _curation_keep_reason(fact) is None
                and reviewed.get(fact.id) != _version(fact)),
               key=lambda fact: (fact.updated_at, fact.id), default=None)


def _save_excerpt(fact, new_content):
    fact.versions.append(fact.snapshot())
    fact.content = new_content
    fact.tags = [tag for tag in fact.tags if normalize(tag) in normalize(new_content)]
    fact.updated_at = time.time()
    fact.review_note = ""
    fact.curation_protected = True


def review_model_key(preset):
    if not preset:
        return ""
    fields = ("type", "model", "base_url", "temperature", "top_p", "presence_penalty",
              "frequency_penalty", "max_tokens", "extra_body", "unsupported_params")
    payload = json.dumps({key: preset.get(key) for key in fields}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class MemoryCuration:
    def __init__(self, profile_id: str, store: LongTermStore):
        self.profile_id = profile_id
        self.store = store
        self._pending: _PendingPreview | None = None
        self._in_flight = asyncio.Lock()
        self._bulk_scope = uuid.uuid4().hex
        self._bulk_page: _BulkPage | None = None

    def _stage_preview(self, versions: dict[str, str], items: list[dict], *, promoted: bool = False) -> dict:
        preview_id = uuid.uuid4().hex
        self._pending = _PendingPreview(preview_id, versions, items, promoted)
        return {"profile_id": self.profile_id, "preview_id": preview_id, "items": deepcopy(items)}

    @staticmethod
    def _cached_review(state, rows, model_key):
        from src.memory.curation_plan import review_key

        entry = state.get("fixed_reviews", {}).get(rows[0].id)
        if entry and entry["key"] == review_key(rows, model_key):
            item = deepcopy(entry["item"])
            item["content"] = rows[0].content
            item["target_content"] = rows[1].content if len(rows) == 2 and item["target_id"] == rows[1].id else None
            return item
        return None

    @staticmethod
    def _remember_review(state, rows, item, model_key):
        from src.memory.curation_plan import review_key

        payload = {k: v for k, v in item.items() if k not in ("content", "target_content")}
        state.setdefault("fixed_reviews", {})[rows[0].id] = {
            "key": review_key(rows, model_key), "item": payload,
        }

    async def _evaluate_fixed(self, decide, snapshot, rows, model_key):
        from src.memory.curation_plan import validate_review

        revision, cursors, facts, state = snapshot
        cached = self._cached_review(state, rows, model_key)
        if cached is not None:
            return snapshot, cached
        item, = validate_review(rows, await decide(rows))
        self._remember_review(state, rows, item, model_key)
        if not self.store.apply_curation(revision, facts, cursors, state):
            raise CurationConflict("Memories changed during the review")
        current = self.store.curation_snapshot()
        if current[2] != facts:
            raise CurationConflict("Memories changed after the review")
        return current, item

    async def preview_fixed(self, decide: Decide, *, model_key="", fact_id=None) -> dict:
        if self._in_flight.locked():
            raise CurationConflict("A memory preview is already running")
        async with self._in_flight:
            snapshot = self.store.curation_snapshot()
            _, _, facts, state = snapshot
            if fact_id is None:
                subject = _pending_fixed_subject(facts, state)
            else:
                subject = next((f for f in facts if f.id == fact_id), None)
                if subject is None or _curation_keep_reason(subject):
                    raise CurationConflict("The memory is unavailable for review")
            if subject is None:
                return self._stage_preview({}, [])
            rows = _fixed_rows(subject, facts)
            _, item = await self._evaluate_fixed(decide, snapshot, rows, model_key)
            return self._stage_preview({f.id: fact_action_revision(f) for f in rows}, [item])

    async def preview_bulk_fixed(self, decide: Decide, *, cursor=None, fact_id=None, model_key="") -> dict:
        """Page a frozen snapshot for free; an explicit focus reviews only that item."""
        if self._in_flight.locked():
            raise CurationConflict("A memory preview is already running")
        async with self._in_flight:
            snapshot = self.store.curation_snapshot()
            _, _, facts, _ = snapshot
            ordered = sorted(facts, key=lambda fact: fact.id)
            versions = {f.id: fact_action_revision(f) for f in ordered}
            if len(versions) != len(ordered):
                raise CurationConflict("Duplicate memory IDs prevent a complete preview")
            signature = hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()
            scope = f"{self._bulk_scope}:{signature}:"
            offset = 0
            if cursor is not None:
                if not cursor.startswith(scope):
                    raise CurationConflict("Start a fresh bulk memory preview")
                try:
                    offset = int(cursor[len(scope):])
                except ValueError as error:
                    raise CurationConflict("Invalid bulk memory preview cursor") from error
                if cursor != f"{scope}{offset}" or not 0 < offset < len(ordered) or offset % _BATCH_SIZE:
                    raise CurationConflict("Invalid bulk memory preview cursor")
            page = ordered[offset:offset + _BATCH_SIZE]
            if fact_id is not None:
                subject = next((f for f in page if f.id == fact_id), None)
                if subject is None or _curation_keep_reason(subject):
                    raise CurationConflict("The selected memory is unavailable for review")
                snapshot, _ = await self._evaluate_fixed(decide, snapshot, _fixed_rows(subject, ordered), model_key)
            revision, _, _, state = snapshot
            items = {}
            for fact in page:
                keep_reason = _curation_keep_reason(fact)
                if keep_reason:
                    items[fact.id] = _item(fact, "keep", keep_reason)
                    continue
                task = _fixed_rows(fact, ordered)
                items[fact.id] = self._cached_review(state, task, model_key) or _item(
                    fact, "pending", "尚未审阅，继续使用 / Unreviewed; remains available",
                )
            covered = offset + len(page)
            page_token = uuid.uuid4().hex
            self._bulk_page = _BulkPage(page_token, self.profile_id, revision, versions, items, fixed=True)
            return {"profile_id": self.profile_id, "page_token": page_token,
                    "items": deepcopy(list(items.values())),
                    "coverage": {"total": len(ordered), "offset": offset, "covered": covered},
                    "page_cursor": cursor,
                    "next_cursor": f"{scope}{covered}" if covered < len(ordered) else None}

    def pending_updates(self, preview_id, fact_ids):
        """Build replacement documents without changing the authoritative snapshot."""
        _, _, facts, state = self.store.curation_snapshot()
        if preview_id in state.get("receipts", {}):
            return []
        pending = self._pending
        if pending is None or pending.id != preview_id:
            raise CurationConflict("Generate a fresh memory preview")
        selected = None if fact_ids is None else set(fact_ids)
        if selected is not None and not selected <= {i["fact_id"] for i in pending.items if i["action"] != "keep"}:
            raise CurationConflict("Selection is not part of this preview")
        by_id = {f.id: f for f in facts}
        changes = []
        for item in pending.items:
            if item["action"] != "update" or (selected is not None and item["fact_id"] not in selected):
                continue
            fid = item["fact_id"]
            fact = by_id.get(fid)
            if fact is None or fact_action_revision(fact) != pending.versions.get(fid) or _curation_keep_reason(fact):
                raise CurationConflict("Memories changed after the preview")
            new = deepcopy(fact)
            _save_excerpt(new, item["new_content"])
            changes.append(new)
        return changes

    async def preview(self, decide: Decide) -> dict:
        if self._in_flight.locked():
            raise CurationConflict("A memory preview is already running")
        async with self._in_flight:
            _, _, facts, state = self.store.curation_snapshot()
            rows = _candidates(facts, state.get("reviewed", {}))
            items = _validate(rows, await decide(rows)) if rows else []
            return self._stage_preview({fact.id: fact_action_revision(fact) for fact in rows}, items)

    async def preview_bulk(self, decide: Decide, *, cursor: str | None = None) -> dict:
        """Read one ID-ordered page from the same store revision. Advice only."""
        if self._in_flight.locked():
            raise CurationConflict("A memory preview is already running")
        async with self._in_flight:
            revision, _, facts, _ = self.store.curation_snapshot()
            facts.sort(key=lambda fact: fact.id)
            if len({fact.id for fact in facts}) != len(facts):
                raise CurationConflict("Duplicate memory IDs prevent a complete bulk preview")
            scope = f"{self._bulk_scope}:{revision}:"
            offset = 0
            if cursor is not None:
                if not isinstance(cursor, str) or not cursor.startswith(scope):
                    raise CurationConflict("Start a fresh bulk memory preview")
                try:
                    offset = int(cursor[len(scope):])
                except ValueError as error:
                    raise CurationConflict("Invalid bulk memory preview cursor") from error
                if cursor != f"{scope}{offset}" or not 0 < offset < len(facts) or offset % _BATCH_SIZE:
                    raise CurationConflict("Invalid bulk memory preview cursor")

            page = facts[offset:offset + _BATCH_SIZE]
            rows, items_by_id = [], {}
            for fact in page:
                reason = _curation_keep_reason(fact)
                if reason is None:
                    rows.append(fact)
                else:
                    items_by_id[fact.id] = _item(fact, "keep", reason)
            if rows:
                items_by_id.update({item["fact_id"]: item for item in _validate(rows, await decide(rows))})
            if self.store.curation_snapshot()[0] != revision:
                raise CurationConflict("Memories changed during the bulk preview")
            covered = offset + len(page)
            page_token = uuid.uuid4().hex
            self._bulk_page = _BulkPage(page_token, self.profile_id, revision,
                                        {fact.id: fact_action_revision(fact) for fact in rows}, items_by_id)
            return {
                "profile_id": self.profile_id,
                "page_token": page_token,
                "items": deepcopy([items_by_id[fact.id] for fact in page]),
                "coverage": {"total": len(facts), "offset": offset, "covered": covered},
                "next_cursor": f"{scope}{covered}" if covered < len(facts) else None,
            }

    def promote_bulk(self, page_token: str, fact_id: str) -> dict:
        """Stage one suggestion from the latest page for the ordinary apply flow."""
        if self._in_flight.locked():
            raise CurationConflict("A memory preview is already running")
        page = self._bulk_page
        if page is None or page.token != page_token or page.profile_id != self.profile_id:
            raise CurationConflict("Generate a fresh bulk memory preview")
        revision, _, facts, _ = self.store.curation_snapshot()
        if revision != page.revision:
            raise CurationConflict("Memories changed after the bulk preview")
        item = page.items.get(fact_id)
        if item is None or item["action"] not in ("archive", "merge", "review", "update"):
            raise CurationConflict("This bulk preview item cannot be promoted")
        selected_ids = [fact_id]
        if item["action"] == "merge":
            target_id = item["target_id"]
            target = page.items.get(target_id)
            if not page.fixed and (target is None or target["action"] != "keep"):
                raise CurationConflict("The bulk merge representative is unavailable")
            selected_ids.append(target_id)
        by_id = {fact.id: fact for fact in facts}
        versions = {}
        for selected_id in selected_ids:
            fact = by_id.get(selected_id)
            version = page.versions.get(selected_id)
            if (version is None or fact is None or _curation_keep_reason(fact) is not None
                    or fact_action_revision(fact) != version):
                raise CurationConflict("Memories changed after the bulk preview")
            versions[selected_id] = version
        return self._stage_preview(versions, [item], promoted=True)

    def apply(self, preview_id: str, fact_ids: list[str] | None = None) -> dict:
        revision, cursors, facts, state = self.store.curation_snapshot()
        selection = sorted(set(fact_ids)) if fact_ids is not None else None
        replay = state.get("receipts", {}).get(preview_id)
        if replay is not None:
            if replay["selection"] != selection:
                raise CurationConflict("This preview was already applied with a different selection")
            return replay["counts"]
        pending = self._pending
        if pending is None or preview_id != pending.id:
            raise CurationConflict("Generate a fresh memory preview")
        if pending.promoted and selection == []:
            raise CurationConflict("Select the promoted memory suggestion")
        versions, items = pending.versions, pending.items
        if selection is not None:
            selected = set(selection)
            if not selected <= {item["fact_id"] for item in items if item["action"] != "keep"}:
                raise CurationConflict("Selection is not part of this preview")
            items = [item for item in items if item["action"] == "keep" or item["fact_id"] in selected]
            relevant_ids = {item["fact_id"] for item in items} | {item["target_id"] for item in items}
            versions = {fact_id: version for fact_id, version in versions.items() if fact_id in relevant_ids}
        receipt = self._commit_preview(preview_id, versions, items, revision, cursors, facts, state,
                                       origin="manual", selection=selection)
        self._pending = None
        return receipt

    def _commit_preview(self, preview_id, versions, items, revision, cursors, facts, state,
                        *, origin, selection=None, offsets=None):
        by_id = {fact.id: fact for fact in facts}
        version_of = fact_action_revision if origin == "manual" else _version
        if any(
            fact_id not in by_id or version_of(by_id[fact_id]) != version or not by_id[fact_id].is_active()
            for fact_id, version in versions.items()
        ):
            raise CurationConflict("Memories changed after the preview")
        receipt = {"ok": True, **dict.fromkeys(_CURATION_COUNTS, 0)}
        for item in items:
            fact = by_id[item["fact_id"]]
            action = item["action"]
            if action == "review":
                if fact.review_note != item["reason"]:
                    fact.review_note = item["reason"]
                    receipt["reviewed"] += 1
                continue
            if not item.get("preserve_review_note"):
                fact.review_note = ""
            if action == "update":
                _save_excerpt(fact, item["new_content"])
                receipt["trimmed"] += 1
            if action in ("archive", "merge"):
                fact.archived = True
                fact.archive_reason = "merged" if action == "merge" else item["archive_reason"]
                fact.archive_note = item["reason"]
                fact.archive_revision = preview_id
                fact.merged_into = item["target_id"]
                if fact.merged_into is not None:
                    fact.merged_source_version = semantic_version(fact)
                    fact.merged_target_version = semantic_version(by_id[fact.merged_into])
                receipt["merged" if action == "merge" else "archived"] += 1
        reviewed = {key: value for key, value in state.get("reviewed", {}).items() if key in by_id}
        reviewed.update({item["fact_id"]: _version(by_id[item["fact_id"]]) for item in items})
        state["reviewed"] = reviewed
        if origin == "manual":
            receipts = state.get("receipts", {})
            receipts[preview_id] = {"selection": selection, "counts": receipt}
            state["receipts"] = dict(list(receipts.items())[-32:])
        elif origin == "daily":
            state["daily"].update({key: receipt[key] for key in _CURATION_COUNTS})
        if receipt["merged"] or receipt["archived"] or receipt["trimmed"]:
            state["index_cleanup_pending"] = preview_id
        state["latest"] = {"counts": receipt, "at": time.time(), "origin": origin}
        saved = (self.store.apply_curation(revision, facts, cursors, state) if offsets is None
                 else self.store.apply_curation_progress(revision, facts, cursors, offsets, state))
        if not saved:
            raise CurationConflict("Memories changed while applying the preview")
        return receipt

    def status(self) -> dict:
        return self.store.curation_status()

    async def run_daily(self, day: str, decide: Decide, *, enabled: Callable[[], bool] = lambda: True,
                        prepare_dialogue=None, decide_combined=None, decide_dialogue=None, model_key=""):
        """Reserve one attempt and commit its validated outputs together."""
        fixed = decide_dialogue is not None
        async with self._in_flight:
            revision, cursors, facts, state, offsets = self.store.curation_progress_snapshot()
            if not enabled() or state.get("daily", {}).get("day", "") >= day:
                return
            dialogue = None
            if prepare_dialogue:
                # An assistant-only prefix cannot supply user evidence. Persist its
                # progress locally and replan once before spending the daily attempt.
                for _ in range(2):
                    batch = prepare_dialogue(facts, cursors, offsets)
                    if batch is None:
                        break
                    if any(row["role"] == "user" for row in batch.rows):
                        dialogue = batch
                        break
                    if not batch.is_current() or not self.store.apply_extraction_progress(
                        revision, facts, batch.next_cursors, batch.next_offsets,
                    ):
                        return
                    revision, cursors, facts, state, offsets = self.store.curation_progress_snapshot()
                    if not enabled() or state.get("daily", {}).get("day", "") >= day:
                        return
            if fixed:
                subject = _pending_fixed_subject(facts, state)
                if dialogue is not None and subject is not None and state.get("daily", {}).get("job") == "dialogue":
                    dialogue = None
                rows = _fixed_rows(subject, facts) if dialogue is None and subject is not None else []
            else:
                rows = _candidates(facts, state.get("reviewed", {}))
            if not rows and dialogue is None:
                return
            run_id = uuid.uuid4().hex
            job = "dialogue" if dialogue is not None else "review"
            cached = self._cached_review(state, rows, model_key) if fixed and rows else None
            state["daily"] = {"day": day, "id": run_id, "status": "running", "job": job,
                              **dict.fromkeys(_DAILY_COUNTS, 0)}
            if not self.store.apply_curation(revision, facts, cursors, state):
                return
            request_token = _daily_request_start.set(lambda: self._record_daily_request(run_id))
            try:
                reviewed_item = None
                if fixed and dialogue is not None:
                    operations, items = await decide_dialogue(dialogue, facts), []
                elif fixed:
                    from src.memory.curation_plan import validate_review

                    operations = []
                    reviewed_item = cached
                    if reviewed_item is None:
                        reviewed_item, = validate_review(rows, await decide(rows))
                    item = reviewed_item
                    # Model-only semantic retirement is available in preview. Daily
                    # execution retains the existing conservative commit boundary.
                    if (item["action"] == "update" or
                        (item["action"] == "archive" and _archive_review_reason(rows[0], item["archive_reason"]))):
                        item = _item(rows[0], "keep", "建议已保存，等待采用的规则验证 / Suggestion saved")
                    items = [{**item, "preserve_review_note": True}] if item["action"] == "keep" else [item]
                else:
                    operations, raw_items = (await decide_combined(rows, dialogue) if dialogue is not None
                                             else ([], await decide(rows)))
                    items = _validate(rows, raw_items)
                if not enabled():
                    self._end_attempt(run_id, "cancelled")
                    return
                revision, current_cursors, current_facts, state, current_offsets = self.store.curation_progress_snapshot()
                if state.get("daily", {}).get("id") != run_id:
                    return
                versions = {fact.id: _version(fact) for fact in rows}
                changed = set()
                next_offsets = None
                if dialogue is not None:
                    if (current_cursors != cursors or current_offsets != offsets or current_facts != facts
                            or not dialogue.is_current()):
                        raise CurationConflict("Memories or dialogue changed during the daily request")
                    changed, items = self._apply_daily_dialogue(dialogue, operations, current_facts, items, versions)
                    current_cursors, next_offsets = dialogue.next_cursors, dialogue.next_offsets
                    state["daily"].update(self._dialogue_counts(dialogue, facts, current_facts))
                if reviewed_item is not None and cached is None:
                    self._remember_review(state, rows, reviewed_item, model_key)
                state["daily"]["status"] = "completed"
                self._commit_preview(run_id, versions, items, revision, current_cursors, current_facts, state,
                                     origin="daily", offsets=next_offsets)
                return changed
            except asyncio.CancelledError:
                self._end_attempt(run_id, "cancelled")
                raise
            except CurationConflict:
                self._end_attempt(run_id, "stale" if dialogue is not None else "failed")
            except Exception:
                self._end_attempt(run_id, "failed")
            finally:
                _daily_request_start.reset(request_token)

    def _record_daily_request(self, run_id: str) -> None:
        revision, cursors, facts, state = self.store.curation_snapshot()
        daily = state.get("daily", {})
        if daily.get("id") != run_id or daily.get("status") != "running" or daily.get("requests") != 0:
            raise CurationConflict("This daily memory request is no longer available")
        daily["requests"] = 1
        if not self.store.apply_curation(revision, facts, cursors, state):
            raise CurationConflict("Memories changed before the daily request")

    @staticmethod
    def _dialogue_counts(dialogue, before, after) -> dict[str, int]:
        counts = {
            "dialogue_messages": len({(row["source"], row["id"]) for row in dialogue.rows}),
            "dialogue_slices": len({row["reference"] for row in dialogue.rows}),
            "added": 0,
            "updated": 0,
            "confirmed": 0,
        }
        by_id = {fact.id: fact for fact in before}
        for fact in after:
            previous = by_id.get(fact.id)
            if previous is None:
                counts["added"] += 1
            elif previous.record_version != fact.record_version:
                counts["updated"] += 1
            elif previous.evidence != fact.evidence:
                counts["confirmed"] += 1
        return counts

    @staticmethod
    def _apply_daily_dialogue(dialogue, operations, facts, items, versions):
        references = {row["reference"]: row for row in dialogue.rows}
        changed = apply_operations(facts, operations, references, dialogue.candidates, dialogue.threshold)
        touched = {fact.id for fact in facts if fact.id in versions and _version(fact) != versions[fact.id]}
        targets = {item["fact_id"] for item in items if item["action"] != "keep"}
        targets.update(item["target_id"] for item in items if item["target_id"] is not None)
        dialogue_targets = {op["fact_id"] for op in operations if op["op"] in ("confirm", "update")}
        if (touched | dialogue_targets) & targets:
            raise CurationConflict("Dialogue and curation change the same record")
        # A keep decision only covered the version shown to the model.
        for fact_id in touched:
            versions.pop(fact_id)
        return changed, [item for item in items if item["fact_id"] not in touched]

    def _end_attempt(self, run_id: str, status: str) -> None:
        revision, cursors, facts, state = self.store.curation_snapshot()
        if state.get("daily", {}).get("id") == run_id:
            state["daily"]["status"] = status
            self.store.apply_curation(revision, facts, cursors, state)

    def restore(self, fact_id: str, archive_revision: str) -> None:
        revision, cursors, facts, state = self.store.curation_snapshot()
        fact = next((fact for fact in facts if fact.id == fact_id), None)
        if fact is None or not fact.archived or fact.archive_revision != archive_revision:
            raise CurationConflict("This archive has changed")
        fact.archived = False
        fact.archive_reason = fact.archive_revision = fact.archive_note = ""
        fact.merged_into = None
        fact.merged_source_version = fact.merged_target_version = ""
        fact.curation_protected = True
        if not self.store.apply_curation(revision, facts, cursors, state):
            raise CurationConflict("Memories changed while restoring the archive")


def curation_for(manager) -> MemoryCuration:
    if getattr(manager, "curation", None) is None:
        manager.curation = MemoryCuration(manager._profile_id, manager.facts)
    return manager.curation


def curation_preset(manager, app) -> dict:
    name = manager._cfg.get("extraction_llm_preset", "")
    config = app.state.config
    preset = config.llm_presets.get(name) if name else config.get_active_llm_preset()
    if not preset or not preset.get("model"):
        raise CurationConflict("Select a memory model before generating suggestions")
    return preset


async def request_fixed_review(rows, preset):
    from src.memory.curation_plan import review_messages

    return (await _request_object(review_messages(rows), preset)).get("items")


async def request_daily_extraction(dialogue_messages, preset):
    system, user = dialogue_messages
    messages = [
        {**system, "content": system["content"] + '\nReturn the array inside one JSON object as {"operations": [...]}. '},
        user,
    ]
    return _bounded_operations(await _request_object(messages, preset))


def _bounded_operations(parsed):
    operations = parsed.get("operations")
    if not isinstance(operations, list) or len(operations) > 24 or any(
        not isinstance(op, dict) or op.get("op") not in ("add", "confirm", "update", "skip") for op in operations
    ):
        raise ValueError("Expected a bounded array of dialogue operations")
    return operations


async def _request_object(messages, preset):
    from src.llm.registry import get_provider
    from src.llm.openai_compat import OpenAICompatProvider

    provider = get_provider(preset)
    if isinstance(provider, OpenAICompatProvider):
        provider.client = provider.client.with_options(max_retries=0, timeout=60.0)
    raw = ""
    try:
        on_request_start = _daily_request_start.get()
        if on_request_start is not None:
            on_request_start()
        async for chunk in provider.stream_chat(messages):
            if chunk.startswith("[LLM Error:") or len(raw) + len(chunk) > 32_000:
                raise ValueError("Memory model request failed")
            raw += chunk
    finally:
        if isinstance(provider, OpenAICompatProvider):
            await provider.client.close()
    raw = raw.strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.partition("\n")[2][:-3].strip()
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Invalid memory curation response")
    return parsed
