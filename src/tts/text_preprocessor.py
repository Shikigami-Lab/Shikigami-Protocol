"""
text_preprocessor.py — Strip content that should not be spoken by TTS.

Removes (in order):
  Code:
  • ```lang...```  — fenced code blocks (entire block, incl. content)
  • `inline code`  — inline backtick code (entire span)
  • \"\"\"...\"\"\"  — triple-quoted strings

  Markdown structure:
  • --- / === / *** lines  — horizontal rules / YAML front-matter separators
  • | table | rows |       — markdown table rows
  • # ## ### headings      — strip # markers, keep heading text
  • - / * / + list items   — strip bullet marker, keep text
  • 1. 2. numbered lists   — strip number+dot, keep text

  Inline annotations:
  • （全角括号内容）  — action/stage directions in Chinese
  • (半角括号内容)    — parenthetical asides (length-capped)
  • *动作描述*        — asterisk-wrapped action text (roleplay)
  • 【标签】          — bracket labels
  • <tag>             — residual HTML/XML tags
  • Emoji             — Unicode emoji blocks (no third-party dependency)
"""

import re

# ─────────────────────────────────────────────────────────────────────────────
# Code blocks & technical noise
# ─────────────────────────────────────────────────────────────────────────────

# Fenced code blocks  ```lang\n...\n```  (multiline, non-greedy)
_CODE_FENCE_RE = re.compile(r"```[\w]*\n?.*?```", re.DOTALL)

# Inline code  `xxx`
_CODE_INLINE_RE = re.compile(r"`[^`\n]{0,200}`")

# Triple double-quotes  """xxx"""  or standalone """
_TRIPLE_QUOTE_RE = re.compile(r'"{3,}[^"]*"{3,}|"{3,}')

# ─────────────────────────────────────────────────────────────────────────────
# Markdown structural noise (whole-line patterns — applied per line)
# ─────────────────────────────────────────────────────────────────────────────

# Horizontal rules: lines that are only -, =, *, _ (3+ chars, optional spaces)
_HR_RE = re.compile(r"^[ \t]*[-=*_]{3,}[ \t]*$", re.MULTILINE)

# Table rows: lines that contain | (with optional leading/trailing spaces)
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.MULTILINE)

# Heading markers: # / ## / ### etc. at line start — strip # symbols, keep text
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)

# Unordered list markers: - / * / + at line start (keep text after)
_UL_MARKER_RE = re.compile(r"^[ \t]*[-*+]\s+", re.MULTILINE)

# Ordered list markers: 1. / 2. etc. at line start (keep text after)
_OL_MARKER_RE = re.compile(r"^[ \t]*\d+\.\s+", re.MULTILINE)

# ─────────────────────────────────────────────────────────────────────────────
# Inline annotation noise
# ─────────────────────────────────────────────────────────────────────────────

_PAREN_ZH_RE    = re.compile(r"（[^）]{0,80}）")           # 全角 （xxx）
_PAREN_EN_RE    = re.compile(r"\([^)]{0,80}\)")             # 半角 (xxx), cap 80 chars
_ASTERISK_RE    = re.compile(r"\*{1,2}[^*\n]{0,80}\*{1,2}")  # *x* or **x**
_BRACKET_ZH_RE  = re.compile(r"【[^】]{0,40}】")            # 【xxx】
_ANGLE_RE       = re.compile(r"<[^>\n]{0,40}>")             # <tag>

# ─────────────────────────────────────────────────────────────────────────────
# Emoji (covers main Unicode blocks without third-party libs)
# ─────────────────────────────────────────────────────────────────────────────

_EMOJI_RE = re.compile(
    "["
    "\U00002600-\U000027FF"   # Misc symbols & Dingbats
    "\U0001F300-\U0001F9FF"   # Misc symbols, Emoticons, Transport, Flags
    "\U0001FA00-\U0001FAFF"   # Chess pieces, Medical symbols, etc.
    "\U00002300-\U000023FF"   # Misc Technical
    "\uFE00-\uFE0F"           # Variation Selectors
    "\u200D"                  # Zero-Width Joiner
    "]+",
    flags=re.UNICODE,
)


def preprocess_for_tts(text: str) -> str:
    """Return a cleaned version of *text* suitable for TTS synthesis."""
    # 1. Code blocks first (must come before inline code to avoid partial matches)
    text = _CODE_FENCE_RE.sub("", text)
    text = _CODE_INLINE_RE.sub("", text)
    text = _TRIPLE_QUOTE_RE.sub("", text)

    # 2. Markdown structural noise
    text = _HR_RE.sub("", text)
    text = _TABLE_ROW_RE.sub("", text)
    text = _HEADING_RE.sub("", text)       # strip #, keep heading text
    text = _UL_MARKER_RE.sub("", text)     # strip - / * / +, keep text
    text = _OL_MARKER_RE.sub("", text)     # strip 1. / 2., keep text

    # 3. Inline annotations
    text = _PAREN_ZH_RE.sub("", text)
    text = _PAREN_EN_RE.sub("", text)
    text = _ASTERISK_RE.sub("", text)
    text = _BRACKET_ZH_RE.sub("", text)
    text = _ANGLE_RE.sub("", text)

    # 4. Emoji
    text = _EMOJI_RE.sub("", text)

    # 5. Collapse blank lines and extra whitespace
    text = re.sub(r"\n{2,}", " ", text)     # multiple blank lines → single space
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()
