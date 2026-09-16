"""SillyTavern-style prompt macro expansion.

Character cards imported from SillyTavern usually write {{char}} and {{user}} instead of
real names in their description / personality / scenario text, and that text is copied
into base_prompt as-is. Without expansion:
  - the model sees a literal "{{char}}" and may repeat it in replies;
  - repeated macros enter chat history, get picked up by memory extraction, and end up in
    facts / day summaries / the user portrait, which are injected back into the prompt,
    so the problem feeds itself.

Expansion therefore runs once at the end of prompt assembly and covers every source:
base_prompt, style constraints, lorebook, custom segments, evolved text and history.

Rules follow SillyTavern: macro names are case-insensitive, whitespace inside the braces
is ignored, and unknown macros are left untouched so braces typed on purpose survive.
A macro whose value is unknown (for example no character name) is also left as-is instead
of silently becoming an empty string.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, Optional

# {{name}}: names are limited to letters, digits and underscores so JSON/template snippets don't match
_MACRO_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# {{trim}} removes itself together with the surrounding newlines, so it runs before normal substitution
_TRIM_RE = re.compile(r"[ \t]*\r?\n?[ \t]*\{\{\s*trim\s*\}\}[ \t]*\r?\n?[ \t]*", re.IGNORECASE)
# Legacy syntax still accepted by SillyTavern. Uppercase only, and only in card text: lowercase
# forms are ordinary markup or code (std::vector<char>), and the system message also carries
# injected material such as search results.
_LEGACY = ((re.compile(r"<USER>"), "user"),
           (re.compile(r"<BOT>"), "char"),
           (re.compile(r"<CHAR>"), "char"))


def build_macro_table(
    char_name: str = "",
    user_name: str = "",
    persona: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    """Build the macro name → value map (keys are lowercase).

    char and user are only included when a name is known, so an unresolved {{char}} stays
    literal. description / personality / scenario have no separate fields in this project
    (imports merge them into base_prompt), so they expand to empty strings, which is still
    better than leaving the literal for the model.
    """
    now = now or datetime.now()
    table = {
        "persona": persona or "",
        "time": now.strftime("%H:%M"),
        "date": now.strftime("%Y-%m-%d"),
        "newline": "\n",
        "description": "",
        "personality": "",
        "scenario": "",
    }
    if char_name:
        table["char"] = char_name
    if user_name:
        table["user"] = user_name
    return table


def expand_macros(text: str, table: Dict[str, str], card_text: bool = True) -> str:
    """Expand the macros in one piece of text; unknown macros are left untouched.

    card_text=False skips {{trim}} and the legacy <USER>/<BOT>/<CHAR> syntax (used for chat messages).
    """
    if not text or ("{{" not in text and (not card_text or "<" not in text)):
        return text

    out = _TRIM_RE.sub("", text) if card_text else text
    if card_text:
        for pattern, key in _LEGACY:
            if key in table:
                out = pattern.sub(lambda _m, value=table[key]: value, out)

    def _sub(m: "re.Match") -> str:
        name = m.group(1).lower()
        return table[name] if name in table else m.group(0)

    return _MACRO_RE.sub(_sub, out)


def expand_messages(messages, table: Dict[str, str]):
    """Expand macros in every message content in place and return the same list.

    System messages (card content) get the full table. User and assistant messages only get
    {{char}} and {{user}}: models copy those from imported cards into their replies, while other
    macros or tags in chat text are usually templates or code typed on purpose.
    """
    names_only = {key: table[key] for key in ("char", "user") if key in table}
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str) and content:
            if msg.get("role") == "system":
                expanded = expand_macros(content, table)
            else:
                expanded = expand_macros(content, names_only, card_text=False)
            if expanded != content:
                msg["content"] = expanded
    return messages


def contains_macros(text: str) -> bool:
    """Whether the text contains expandable macros (for diagnostics; the text is not modified)."""
    if not text:
        return False
    if any(p.search(text) for p, _ in _LEGACY):
        return True
    return bool(_MACRO_RE.search(text)) or bool(_TRIM_RE.search(text))
