"""Bound one extraction request without skipping unprocessed dialogue."""

from hashlib import sha256
from heapq import merge

MAX_DIALOGUE_CHARS = 32_000
MAX_SLICE_CHARS = 6_000


def is_memory_turn(message):
    """Accept user speech and completed assistant replies."""
    def value(key, default=None):
        return message.get(key, default) if isinstance(message, dict) else getattr(message, key, default)
    role = value("role")
    return role == "user" or (role == "assistant" and value("generation_status", "completed") == "completed")


def message_hash(content):
    return sha256(content.encode("utf-8")).hexdigest()


def memory_source_excerpt(row, reference):
    """Resolve a stored reference against the current message and its original span."""
    if (row is None or not is_memory_turn(row) or row["timestamp"] != reference.get("timestamp")
            or row["role"] != reference.get("speaker_role", reference.get("role", "user"))):
        return None
    content = row["content"]
    if reference.get("content_hash") and message_hash(content) != reference["content_hash"]:
        return None
    start = reference.get("start_char", 0)
    end = reference.get("end_char", len(content))
    if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(content):
        return None
    return content, start, end


def plan_batch(streams, cursors, offsets, *, limit=MAX_DIALOGUE_CHARS):
    """Select a bounded prefix from each source and record exactly what was covered.

    A cursor advances past a message only after the last character is included.
    The offset points to the next character in an unfinished, unchanged message.
    """
    parts = []
    originals = []
    next_cursors, next_offsets = dict(cursors), dict(offsets)
    remaining = limit

    def chronology(row):
        return row["timestamp"], row["source"], row["id"]

    for row in merge(*streams.values(), key=chronology):
        source, id = row["source"], row["id"]
        content = row["content"]
        if not is_memory_turn(row) or not content:
            next_cursors[source] = id
            next_offsets.pop(source, None)
            continue
        if remaining <= 0:
            break

        digest = message_hash(content)
        pending = offsets.get(source, {})
        start = pending.get("offset", 0) if pending.get("id") == id and pending.get("content_hash") == digest else 0
        if not 0 <= start < len(content):
            start = 0
        originals.append(row)
        while start < len(content) and remaining > 0:
            end = min(len(content), start + MAX_SLICE_CHARS, start + remaining)
            ref = f"{source}:{id}"
            part = {**row, "content": content[start:end], "reference": ref, "content_hash": digest}
            if start != 0 or end != len(content):
                part.update(reference=f"{ref}#{start}-{end}", start_char=start, end_char=end)
            parts.append(part)
            remaining -= end - start
            start = end

        if start == len(content):
            next_cursors[source] = id
            next_offsets.pop(source, None)
        else:
            next_offsets[source] = {"id": id, "offset": start, "content_hash": digest}
            break

    return parts, originals, next_cursors, next_offsets
