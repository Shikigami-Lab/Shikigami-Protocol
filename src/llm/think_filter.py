"""剥离 LLM 输出中内联的 <think>...</think> 思维链。

MiniMax M2/M3、部分本地部署的推理模型（Ollama 上的 Qwen3 等）走 OpenAI
兼容接口时，不使用独立的 reasoning_content 字段，而是把思考内容直接混在
content 里用 <think> 标签包住。不剥掉的话会泄露到前端，还会被写进对话
历史、污染记忆抽取和情绪分类的输入。
"""

OPEN_TAG = "<think>"
CLOSE_TAG = "</think>"


def _partial_tag_len(text: str, tag: str) -> int:
    """text 结尾有多长的后缀可能是 tag 的开头（半个标签跨 chunk 的情况）。"""
    limit = min(len(text), len(tag) - 1)
    for n in range(limit, 0, -1):
        if tag.startswith(text[-n:]):
            return n
    return 0


class ThinkTagFilter:
    """流式过滤器：吞掉 <think>...</think> 之间的内容，其余原样放行。

    标签可能被拆在相邻 chunk 的边界上，所以凡是结尾疑似半个标签的部分
    先扣在缓冲区里，等下一个 chunk 拼上再判断。流结束后必须调 flush()
    取回缓冲区里剩余的正文。
    """

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""
        self._emitted = False  # 是否已输出过可见正文（用于吞掉开头 think 块后的空白）

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out = []
        while True:
            if self._in_think:
                idx = self._buf.find(CLOSE_TAG)
                if idx == -1:
                    # 思考内容直接丢弃，只留可能是半个闭合标签的尾巴
                    keep = _partial_tag_len(self._buf, CLOSE_TAG)
                    self._buf = self._buf[-keep:] if keep else ""
                    break
                self._buf = self._buf[idx + len(CLOSE_TAG):]
                self._in_think = False
            else:
                if not self._emitted:
                    self._buf = self._buf.lstrip()
                    if not self._buf:
                        break
                idx = self._buf.find(OPEN_TAG)
                if idx == -1:
                    keep = _partial_tag_len(self._buf, OPEN_TAG)
                    piece, self._buf = (self._buf[:-keep], self._buf[-keep:]) if keep else (self._buf, "")
                    if piece:
                        out.append(piece)
                        self._emitted = True
                    break
                if idx > 0:
                    out.append(self._buf[:idx])
                    self._emitted = True
                self._buf = self._buf[idx + len(OPEN_TAG):]
                self._in_think = True
        return "".join(out)

    def flush(self) -> str:
        """流结束：缓冲区里未闭合的思考内容（多半被 max_tokens 截断）丢弃，正文放行。"""
        buf, self._buf = self._buf, ""
        if self._in_think:
            return ""
        return buf


def strip_think_tags(text: str) -> str:
    """非流式版本，语义与 ThinkTagFilter 一致（含未闭合标签的截断处理）。"""
    if OPEN_TAG not in text:
        return text
    flt = ThinkTagFilter()
    return flt.feed(text) + flt.flush()
