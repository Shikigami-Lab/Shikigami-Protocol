"""Proactive topics backed by original single-chat user statements."""

import logging
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from heapq import nlargest
from itertools import zip_longest
from typing import Optional

from src.core.topics.base import (TopicCandidate, TopicMaterial, TopicSource,
                                  TopicSourceContext)
from src.core.topics.registry import register_topic_source
from src.memory.conversation_store import ConversationStore
from src.memory.extraction_batches import MAX_DIALOGUE_CHARS, memory_source_excerpt, message_hash
from src.memory.long_term_store import _valid_experience_range
from src.memory.recall import semantic_version

logger = logging.getLogger(__name__)

_MIN_AGE_DAYS = 2
_RECENT_MAX_DAYS = 14
_MAX_AGE_DAYS = 180
_MAX_ROWS_PER_DAY = 16
_MAX_CANDIDATES = 6
_MAX_CONTENT_CHARS = 600
_MAX_LINKED = 2
_MAX_FACTS_CHECKED = 32
_MAX_REFS_PER_FACT = 16
_FACT_ID = r"[A-Za-z0-9_-]{1,80}"
_ITEM_ID = re.compile(
    r"(?P<origin>(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})\.(?P<message>[1-9][0-9]{0,18})\.[0-9a-f]{16})"
    rf"(?:\.exp\.(?P<fact>{_FACT_ID})\.(?P<version>[0-9a-f]{{16}})"
    r"\.(?P<start>[0-9]{1,5})-(?P<end>[0-9]{1,5}))?"
)

# High-precision examples of completed actions. Unfamiliar or ambiguous wording
# can be skipped. A lexical match only supports quoting what the user said.
_COMPLETED_ACTION = re.compile(
    r"^(?:I(?: have|'ve|’ve)? (?:just )?"
    r"(?:finished|completed|submitted|passed|visited|attended|watched|cooked|made|"
    r"went to|got back from|returned from|met|built|fixed|shipped|won)\s+\w"
    r"|(?:昨天|前天|上周|上个月)?我(?:昨天|前天|上周|上个月)?(?:刚刚?|已经)?"
    r"(?:完成了|做完了|写完了|看完了|跑完了|修好了|交完了|提交了|通过了|参加了|"
    r"参观了|去了|去过|看了|吃了|做了|见到了|拿到了|赢得了|收到了)\s*\w)",
    re.IGNORECASE,
)
_UNSUITABLE = re.compile(
    r"[?？%％]|\d\s*[/／]\s*\d|\b\d+\s+(?:out of|of)\s+\d+\b"
    r"|\b(?:will|would|might|tomorrow|next\s+(?:week|month|year)|about to|"
    r"plan(?:ned|s)? to|planning|going to|want to|hope to|intend to|need to|"
    r"still|so far|ongoing|in progress|halfway|half of|first draft|first (?:part|chapter|phase)|"
    r"remaining|countdown|until|to go|(?:days?|hours?|minutes?|weeks?) left|"
    r"(?:day|week|chapter|phase|part)\s+\d+|"
    r"if|pretend(?:ed|ing)?|role[ -]?play(?:ing)?|fiction(?:al)?|my character|"
    r"in (?:the story|a dream|my dream))\b"
    r"|明天|后天|下周|下个月|计划|打算|准备|接下来|即将|将要|想要|"
    r"[一二三四五六七八九十百零两0-9]+(?:天|周|个月|小时|分钟)后|"
    r"还[在没剩有差得要需]|剩[余下]|倒计时|距离|只差|进度|进行中|做到|"
    r"分之|一部分|一半|初稿|第[一二三四五六七八九十百零两0-9]+[步章阶段天]|"
    r"如果|假如|假装|设想|扮演|剧情|虚构|梦里",
    re.IGNORECASE,
)
_LINKED_UNSUITABLE = re.compile(
    r"\b(?:plans?|considering|thinking (?:of|about)|supposed to|scheduled (?:to|for)|"
    r"looking forward to|not yet|should|may|sometime in the future|never|"
    r"(?:have|has|did)(?: not|n['’]t)|imagin(?:e|ed|ing)|hypothetical|dream(?:ed|t))\b"
    r"|\b(?:am|are|is|['’]m|['’]re|['’]s)\s+\w+ing\b"
    r"|想去|想做|考虑|预计|拟于|预约|尚未|未完成|没有去|没去|想象|幻想|梦见|假设",
    re.IGNORECASE,
)


def _cached_store(ctx: TopicSourceContext) -> Optional[ConversationStore]:
    session = ctx.session
    if (getattr(session, "id", None) != ctx.profile_id
            or getattr(session, "profile_id", None) != ctx.profile_id):
        return None
    # Session.conversation_store is lazy and can create or migrate profile files.
    store = getattr(session, "_store", None)
    return store if getattr(store, "_conn", None) is not None else None


def _day_start(day: date) -> float:
    return datetime.combine(day, time.min).timestamp()


def _quotable(content: str) -> bool:
    return (isinstance(content, str) and len(content) <= _MAX_CONTENT_CHARS
            and content.isprintable() and bool(content.strip()))


def _eligible_text(content: str) -> bool:
    return (_quotable(content) and _COMPLETED_ACTION.match(content.lstrip()) is not None
            and _UNSUITABLE.search(content) is None)


def _item_id(profile_id: str, day: date, message_id: int, timestamp: float, content: str) -> str:
    revision = message_hash(f"{profile_id}\0{timestamp!r}\0{content}")[:16]
    return f"{day.isoformat()}.{message_id}.{revision}"


def _preview(day, content):
    text = content.strip()
    preview = text[:90] + ("…" if len(text) > 90 else "")
    return f"{day} · “{preview}”"


def _safe_experience(fact):
    return (fact is not None and fact.kind == "experience" and fact.is_active()
            and not fact.event_date and fact.expires_at is None
            and _valid_experience_range(fact.occurred_from, fact.occurred_to)
            and isinstance(fact.content, str) and bool(fact.content.strip())
            and _UNSUITABLE.search(fact.content) is None and _LINKED_UNSUITABLE.search(fact.content) is None)


def _user_refs(fact):
    for ref in fact.evidence[:_MAX_REFS_PER_FACT]:
        if not isinstance(ref, dict) or ref.get("source") != "single":
            continue
        message_id = ref.get("id")
        if (type(message_id) is int and 0 < message_id <= 2**63 - 1
                and ref.get("speaker_role", ref.get("role")) == "user"
                and ref.get("assertion", "self_statement") == "self_statement"
                and isinstance(ref.get("content_hash"), str) and ref["content_hash"]):
            yield ref


def _linked_excerpt(row, ref):
    if (row is None or row["role"] != "user" or row["generation_status"] != "completed"
            or not isinstance(row["content"], str) or len(row["content"]) > MAX_DIALOGUE_CHARS):
        return None
    excerpt = memory_source_excerpt(row, ref)
    if excerpt is None:
        return None
    content, start, end = excerpt
    # A slice cannot omit a plan or roleplay cue elsewhere in the original message.
    if (_UNSUITABLE.search(content) is not None or _LINKED_UNSUITABLE.search(content) is not None
            or not _quotable(content[start:end])):
        return None
    return content[start:end], start, end


@register_topic_source
class ConversationRecallTopicSource(TopicSource):
    source_id = "conversation_recall"
    label = "对话回忆"
    label_en = "Conversation Recall"
    enabled_by_default = True
    framing_hint = ("把素材当作用户以前说过的原话，日期是发言日期。自然聊起这段经历，"
                    "只引用原话支持的细节。")
    framing_hint_en = ("Treat the material as something the user said earlier, dated by when "
                       "they wrote it. Revisit it naturally and stick to details their words support.")

    def _used_origins(self, ctx):
        prefix = f"{self.source_id}:"
        return {match["origin"] for ref in ctx.recently_used
                if isinstance(ref, str) and ref.startswith(prefix)
                and (match := _ITEM_ID.fullmatch(ref[len(prefix):]))}

    def _linked_candidates(self, ctx, store, today, used_origins, occupied_days):
        facts = getattr(ctx.memory_manager, "facts", None)
        if facts is None:
            return []
        try:
            active = facts.get_active()
        except OSError:
            logger.debug("[topics.conversation_recall] Could not read structured experiences")
            return []
        experiences = (fact for fact in active if fact.kind == "experience")
        earliest = _day_start(today - timedelta(days=_MAX_AGE_DAYS))
        latest = _day_start(today - timedelta(days=_MIN_AGE_DAYS - 1))
        candidates = []
        for fact in nlargest(_MAX_FACTS_CHECKED, experiences, key=lambda fact: (fact.updated_at, fact.id)):
            if not _safe_experience(fact) or not re.fullmatch(_FACT_ID, fact.id):
                continue
            for ref in _user_refs(fact):
                timestamp = ref.get("timestamp")
                if type(timestamp) not in (int, float) or not earliest <= timestamp < latest:
                    continue
                message_id = ref["id"]
                row = store.get_memory_source_message(message_id)
                excerpt = _linked_excerpt(row, ref)
                if excerpt is None:
                    continue
                day = datetime.fromtimestamp(timestamp).date()
                origin = _item_id(ctx.profile_id, day, message_id, row["timestamp"], row["content"])
                if origin not in used_origins and day not in occupied_days:
                    quote, start, end = excerpt
                    item_id = f"{origin}.exp.{fact.id}.{semantic_version(fact)[:16]}.{start}-{end}"
                    candidates.append(TopicCandidate(self.source_id, item_id, _preview(day, quote)))
                    used_origins.add(origin)
                    occupied_days.add(day)
                # Keep one original anchor per fact, including across cooldowns.
                break
            if len(candidates) == _MAX_LINKED:
                break
        return candidates

    def _candidates_by_day(self, ctx, store, today, first_age, last_age, used_origins, occupied_days):
        for age in range(first_age, last_age + 1):
            day = today - timedelta(days=age)
            if day in occupied_days:
                continue
            # Use the existing connection for the bounded range query. Opening even
            # a mode=ro SQLite connection can create WAL sidecar files on a cold DB.
            rows = store._conn.execute(
                "SELECT id, timestamp, CASE WHEN length(CAST(content AS BLOB)) <= ? THEN content END "
                "FROM messages "
                "WHERE timestamp >= ? AND timestamp < ? AND role = 'user' "
                "AND generation_status = 'completed' "
                "ORDER BY timestamp DESC, id DESC LIMIT ?",
                (4 * _MAX_CONTENT_CHARS, _day_start(day),
                 _day_start(day + timedelta(days=1)), _MAX_ROWS_PER_DAY),
            ).fetchall()
            for message_id, timestamp, content in rows:
                if not _eligible_text(content):
                    continue
                item_id = _item_id(ctx.profile_id, day, message_id, timestamp, content)
                if item_id in used_origins:
                    continue
                yield TopicCandidate(self.source_id, item_id, _preview(day, content))
                break

    def get_candidates(self, ctx: TopicSourceContext) -> list[TopicCandidate]:
        store = _cached_store(ctx)
        if store is None:
            return []
        today = date.today()
        used_origins, occupied_days = self._used_origins(ctx), set()
        try:
            candidates = self._linked_candidates(ctx, store, today, used_origins, occupied_days)
            recent = self._candidates_by_day(ctx, store, today, _MIN_AGE_DAYS, _RECENT_MAX_DAYS,
                                             used_origins, occupied_days)
            older = self._candidates_by_day(ctx, store, today, _RECENT_MAX_DAYS + 1, _MAX_AGE_DAYS,
                                            used_origins, occupied_days)
            # Alternate eligible recent and older days even when one window is sparse.
            for pair in zip_longest(recent, older):
                for candidate in pair:
                    if candidate is not None:
                        candidates.append(candidate)
                        if len(candidates) == _MAX_CANDIDATES:
                            return candidates
        except sqlite3.Error:
            logger.debug("[topics.conversation_recall] Could not read original history")
            return []
        return candidates

    def resolve(self, item_id: str, ctx: TopicSourceContext) -> Optional[TopicMaterial]:
        match = _ITEM_ID.fullmatch(item_id) if isinstance(item_id, str) else None
        if match is None or match["origin"] in self._used_origins(ctx):
            return None
        store = _cached_store(ctx)
        if store is None:
            return None
        message_id = int(match["message"])
        if message_id > 2**63 - 1:
            return None
        try:
            day = date.fromisoformat(match["date"])
            if not _MIN_AGE_DAYS <= (date.today() - day).days <= _MAX_AGE_DAYS:
                return None
            row = store.get_memory_source_message(message_id)
            if row is None or row["role"] != "user" or row["generation_status"] != "completed":
                return None
            timestamp, content = row["timestamp"], row["content"]
            if (not _day_start(day) <= timestamp < _day_start(day + timedelta(days=1))
                    or not isinstance(content, str) or len(content) > MAX_DIALOGUE_CHARS):
                return None
            if _item_id(ctx.profile_id, day, message_id, timestamp, content) != match["origin"]:
                return None
            meta = {"date": day.isoformat(), "source": "single", "message_id": message_id}
            if match["fact"] is None:
                if _eligible_text(content):
                    return TopicMaterial(self.source_id, item_id, f"{day} · “{content}”", meta=meta)
                return None

            facts = getattr(ctx.memory_manager, "facts", None)
            fact = facts.get_by_id(match["fact"]) if facts is not None else None
            if not _safe_experience(fact) or semantic_version(fact)[:16] != match["version"]:
                return None
            expected_span = (int(match["start"]), int(match["end"]))
            for ref in _user_refs(fact):
                excerpt = _linked_excerpt(row, ref) if ref["id"] == message_id else None
                if excerpt is not None and excerpt[1:] == expected_span:
                    return TopicMaterial(self.source_id, item_id, f"{day} · “{excerpt[0]}”",
                                         meta={**meta, "experience_id": fact.id})
            return None
        except (sqlite3.Error, OSError, ValueError, TypeError):
            logger.debug("[topics.conversation_recall] Could not resolve original history")
            return None
