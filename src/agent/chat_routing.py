"""One-call chat routing for OpenAI-compatible and text-only providers."""

import json


OPEN = "<shikigami-task>"
CLOSE = "</shikigami-task>"
MAX_HANDOFF = 4096

DISPATCH_INSTRUCTION = """[Task delegation]
Answer ordinary conversation and ask any necessary clarification as usual.
If you decide to execute a real task with the available tools, delegate it by
outputting only <shikigami-task>{"goal":"task objective and constraints"}</shikigami-task>.
Use valid JSON and a concise goal (at most 1000 characters). Never quote this
format in a conversational reply. Never claim to have executed a delegated task."""


async def route_chat_reply(tokens, *, enabled=True, allow_delegation=True):
    """Stream speech, withholding any control frame until the response ends."""
    pending = ""
    handoff = False
    spoke = False
    async for token in tokens:
        if token.startswith("[LLM Error:"):
            raise ValueError("Chat model could not finish the reply.")
        if not enabled:
            yield token, None
            continue
        pending += token
        if not handoff:
            prefix, marker, _ = pending.partition(OPEN)
            if marker:
                if spoke or prefix.strip() or not allow_delegation:
                    raise ValueError("Chat model returned an invalid task handoff.")
                pending = pending.lstrip()
                handoff = True
            else:
                start = pending.rfind("<")
                if start >= 0 and OPEN.startswith(pending[start:]):
                    visible, pending = pending[:start], pending[start:]
                else:
                    visible, pending = pending, ""
                if visible.strip() or spoke:
                    spoke = True
                    if visible:
                        yield visible, None
                else:
                    pending = visible + pending
        if len(pending) > MAX_HANDOFF:
            raise ValueError("Chat model returned an invalid task handoff.")
    if not handoff:
        if pending.lstrip().startswith("<shikigami-"):
            raise ValueError("Chat model returned an incomplete task handoff.")
        if pending:
            yield pending, None
        return
    try:
        candidate = pending.strip()
        if not candidate.endswith(CLOSE):
            raise ValueError
        value = json.loads(candidate[len(OPEN):-len(CLOSE)])
        goal = value.get("goal") if isinstance(value, dict) else None
        if not isinstance(goal, str) or not 0 < len(goal.strip()) <= 1000:
            raise ValueError
    except ValueError:
        raise ValueError("Chat model returned an invalid task handoff.") from None
    yield "", goal.strip()
