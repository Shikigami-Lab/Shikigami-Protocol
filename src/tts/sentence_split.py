"""TTS 分句：按中英文标点将长文本切成短句，用于 Qwen3 等逐段合成以降低单次推理长度、加快首包。"""
import re
from typing import List

# 句末标点（保留在句末）：中文 。！？； 英文 .!?; 以及换行
_SENTENCE_END_RE = re.compile(r"([。！？；.!?;\n])", re.UNICODE)


def split_sentences_for_tts(text: str, max_chars: int = 0) -> List[str]:
    """按标点分句，返回非空句子列表（每句保留末尾标点）。

    - 在 。！？；.!?; 及换行处切分，标点保留在前一句末尾。
    - 若 max_chars > 0，超过 max_chars 的单句会按 max_chars 再切（避免单句过长）。
    """
    if not (text and text.strip()):
        return []
    text = text.strip()
    # split 并保留分隔符（标点），在列表中会交替出现 内容、标点、内容、标点...
    parts = _SENTENCE_END_RE.split(text)
    sentences: List[str] = []
    current: List[str] = []
    for i, p in enumerate(parts):
        if _SENTENCE_END_RE.fullmatch(p):
            # 标点：接到当前句末尾
            current.append(p)
            sent = "".join(current).strip()
            if sent:
                if max_chars > 0 and len(sent) > max_chars:
                    for j in range(0, len(sent), max_chars):
                        chunk = sent[j : j + max_chars].strip()
                        if chunk:
                            sentences.append(chunk)
                else:
                    sentences.append(sent)
            current = []
        else:
            # 非标点：可能是正常内容或空（开头/连续标点）
            if p.strip():
                current.append(p)
    # 末尾无标点的一段
    if current:
        sent = "".join(current).strip()
        if sent:
            if max_chars > 0 and len(sent) > max_chars:
                for j in range(0, len(sent), max_chars):
                    chunk = sent[j : j + max_chars].strip()
                    if chunk:
                        sentences.append(chunk)
            else:
                sentences.append(sent)
    return sentences
