"""SillyTavern 风格的 prompt 宏展开。

从 SillyTavern 导入的角色卡，description / personality / scenario 里普遍用
{{char}} 和 {{user}} 代替具体名字。这些文本会被原样搬进 base_prompt，如果不展开：
  - 模型看到字面的「{{char}}」，轻则困惑，重则照着念出来
  - 念出来的内容进对话历史 → 被记忆抽取 → 写进 facts / 日摘要 / 用户画像
    → 又被注入 prompt，形成放大循环

因此在 prompt 组装的最后统一展开一次，覆盖 base_prompt、风格约束、世界书、
自定义段落、演化后的文本以及历史消息等所有来源。

规则对齐 SillyTavern：宏名大小写不敏感，花括号内的空白忽略，
未登记的宏原样保留（避免吞掉用户有意写下的花括号内容）。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Dict, Optional

# {{name}}：宏名只允许字母数字与下划线，避免误吞 JSON/模板片段
_MACRO_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# {{trim}} 语义是「连同周围换行一起去掉」，需在普通替换之前单独处理
_TRIM_RE = re.compile(r"[ \t]*\r?\n?[ \t]*\{\{\s*trim\s*\}\}[ \t]*\r?\n?[ \t]*", re.IGNORECASE)
# 旧语法，SillyTavern 仍兼容
_LEGACY = ((re.compile(r"<USER>", re.I), "user"),
           (re.compile(r"<BOT>", re.I), "char"),
           (re.compile(r"<CHAR>", re.I), "char"))


def build_macro_table(
    char_name: str = "",
    user_name: str = "",
    persona: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    """构建宏名 → 展开值的映射（键一律小写）。

    description / personality / scenario 在本项目里没有独立字段（导入时已合并进
    base_prompt），展开为空串，总好过把字面量留给模型。
    """
    now = now or datetime.now()
    return {
        "char": char_name or "",
        "user": user_name or "",
        "persona": persona or "",
        "time": now.strftime("%H:%M"),
        "date": now.strftime("%Y-%m-%d"),
        "newline": "\n",
        "description": "",
        "personality": "",
        "scenario": "",
    }


def expand_macros(text: str, table: Dict[str, str]) -> str:
    """按 table 展开一段文本里的宏；未登记的宏原样保留。"""
    if not text or ("{{" not in text and "<" not in text):
        return text

    out = _TRIM_RE.sub("", text)
    for pattern, key in _LEGACY:
        if key in table:
            out = pattern.sub(table[key], out)

    def _sub(m: "re.Match") -> str:
        name = m.group(1).lower()
        return table[name] if name in table else m.group(0)

    return _MACRO_RE.sub(_sub, out)


def expand_messages(messages, table: Dict[str, str]):
    """就地展开一个 messages 列表里所有 content 的宏，返回同一个列表。"""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str) and content:
            expanded = expand_macros(content, table)
            if expanded != content:
                msg["content"] = expanded
    return messages


def contains_macros(text: str) -> bool:
    """是否含可展开的宏（供诊断/统计用，不改动文本）。"""
    if not text:
        return False
    if any(p.search(text) for p, _ in _LEGACY):
        return True
    return bool(_MACRO_RE.search(text)) or bool(_TRIM_RE.search(text))
