"""Strip inline <think>...</think> reasoning blocks from LLM output.

Some OpenAI-compatible backends (MiniMax M2/M3, reasoning models such as Qwen3 served by
Ollama) don't use a separate reasoning_content field; they put the reasoning into content,
wrapped in <think> tags. Left in place it would reach the frontend and be written into chat
history, polluting memory extraction and emotion classification.

Only a block that starts before any visible text counts as reasoning. A "<think>" that
appears later is ordinary text (for example a reply that talks about the tag) and passes
through unchanged. Tags are matched case-insensitively.

Known limitation: some chat templates insert the opening <think> themselves, so the output
starts with reasoning and only a closing </think> arrives. A streaming filter can't tell
that text is reasoning before it has already been emitted, so that case is not handled.
"""
import re

OPEN_TAG = "<think>"
CLOSE_TAG = "</think>"
_CLOSE_RE = re.compile(re.escape(CLOSE_TAG), re.IGNORECASE)


def _partial_tag_len(text: str, tag: str) -> int:
    """How many trailing characters of text could be the start of tag (a tag split across chunks)."""
    limit = min(len(text), len(tag) - 1)
    for n in range(limit, 0, -1):
        if tag.startswith(text[-n:].lower()):
            return n
    return 0


class ThinkTagFilter:
    """Streaming filter: drops leading <think>...</think> blocks and passes everything else through.

    A tag can be split across chunk boundaries, so a tail that might be part of a tag is held
    back until the next chunk arrives. Call flush() when the stream ends to get what is left.
    """

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""
        self._emitted = False  # visible text went out; from here on everything is plain text

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out = []
        while self._buf:
            if self._in_think:
                m = _CLOSE_RE.search(self._buf)
                if m is None:
                    # Drop the reasoning; keep only a tail that may be half a closing tag
                    keep = _partial_tag_len(self._buf, CLOSE_TAG)
                    self._buf = self._buf[-keep:] if keep else ""
                    break
                self._buf = self._buf[m.end():]
                self._in_think = False
                continue
            if self._emitted:
                out.append(self._buf)
                self._buf = ""
                break
            self._buf = self._buf.lstrip()
            if not self._buf:
                break
            if self._buf[:len(OPEN_TAG)].lower() == OPEN_TAG:
                self._buf = self._buf[len(OPEN_TAG):]
                self._in_think = True
                continue
            if OPEN_TAG.startswith(self._buf.lower()):
                break  # may still become <think>; wait for more input
            out.append(self._buf)
            self._buf = ""
            self._emitted = True
        return "".join(out)

    def flush(self) -> str:
        """End of stream: an unclosed reasoning block (usually cut off by max_tokens) is dropped."""
        buf, self._buf = self._buf, ""
        return "" if self._in_think else buf


def strip_think_tags(text: str) -> str:
    """Non-streaming version with the same semantics as ThinkTagFilter."""
    if OPEN_TAG not in text.lower():
        return text
    flt = ThinkTagFilter()
    return flt.feed(text) + flt.flush()
