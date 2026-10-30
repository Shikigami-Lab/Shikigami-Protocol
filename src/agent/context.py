"""Pure text context envelopes for offline Harness protocol validation."""

from src.llm.message_normalization import normalize_messages


def build_turn_context(
    *, profile_id: str, request_id: str, context_revision: int,
    origin: str, messages: list,
) -> dict:
    """Materialize provenance before projecting messages to JSON text fields.

    IDs are opaque, preserved strings of 1..256 characters (not whitespace-only);
    they are never paths. Require exactly one system and a final non-system,
    nonblank user message carrying this turn's trigger, including reflections.
    Only role/content dictionary keys are accepted; PromptMessage attributes
    remain local until normalization renders the reserved history time index.
    Native attachments and tool protocol messages are deliberately unsupported.

    Origin is explicit metadata, never inferred from text or folded into model
    speech. This envelope is not authentication: the external control layer must
    authenticate identity and authorize profile/origin/revision independently.
    No I/O, database mutation, model calls, or input mutation occurs here.
    Invalid values raise ValueError.
    """
    for name, value in (("profile_id", profile_id), ("request_id", request_id)):
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError(f"{name} must be a nonblank string of at most 256 characters")
    if (isinstance(context_revision, bool) or not isinstance(context_revision, int)
            or not 0 <= context_revision <= 2**53 - 1):
        raise ValueError("context_revision must be a nonnegative JavaScript-safe integer, not bool")
    if not isinstance(origin, str) or origin not in ("user", "reflection"):
        raise ValueError("origin must be user or reflection")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a nonempty list")

    systems = 0
    last_turn = None
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise ValueError(f"messages[{index}] must contain only role and content")
        role, content = message["role"], message["content"]
        if not isinstance(role, str) or role not in ("system", "user", "assistant"):
            raise ValueError(f"messages[{index}] has an unsupported role")
        if not isinstance(content, str):
            raise ValueError(f"messages[{index}].content must be a string")

        # Validate private provenance without discarding it before normalization.
        sources = getattr(message, "time_sources", ())
        if not isinstance(sources, (tuple, list)) or any(not isinstance(s, str) for s in sources):
            raise ValueError(f"messages[{index}] has invalid time_sources")
        span = getattr(message, "time_index_span", None)
        if span is not None:
            if (role != "system" or not isinstance(span, (tuple, list)) or len(span) != 2
                    or any(isinstance(n, bool) or not isinstance(n, int) for n in span)
                    or not 0 <= span[0] <= span[1] <= len(content)):
                raise ValueError(f"messages[{index}] has an invalid time_index_span")
        if role == "system":
            systems += 1
        else:
            last_turn = message

    if systems != 1:
        raise ValueError("messages must contain exactly one system message")
    if last_turn is None or last_turn["role"] != "user" or not last_turn["content"].strip():
        raise ValueError("the final non-system message must be a nonblank user trigger")

    # Keep PromptMessage provenance alive until merging and time rendering finish.
    normalized = normalize_messages(messages)
    return {
        "schema_version": 1,
        "profile_id": profile_id,
        "request_id": request_id,
        "context_revision": context_revision,
        "origin": origin,
        "messages": [{"role": m["role"], "content": m["content"]} for m in normalized],
    }
