import logging
import traceback
from typing import AsyncGenerator, Dict, List, Any

from openai import AsyncOpenAI

from src.llm.base import LLMProvider
from src.utils.debug_logger import log_error, log_debug

logger = logging.getLogger(__name__)


def _normalize_messages(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Normalize messages to satisfy strict user/assistant alternation required by
    local model Jinja templates (Qwen3, Mistral, etc.).

    Pass 1 — merge consecutive same-role messages (excluding system):
        Handles ASE proactive turns that stack multiple assistant entries in a row.
    Pass 2 — ensure first non-system message is user:
        If the session opened with an ASE greeting before the user ever replied,
        insert a minimal placeholder user turn so the template does not reject
        the conversation while still preserving the assistant's opening content.
    """
    if not messages:
        return messages

    # Pass 1: merge consecutive same-role messages
    merged: List[Dict[str, str]] = []
    for msg in messages:
        if (merged
                and msg["role"] == merged[-1]["role"]
                and msg["role"] != "system"):
            merged[-1] = {**merged[-1], "content": merged[-1]["content"] + "\n\n" + (msg["content"] or "")}
        else:
            merged.append(dict(msg))

    # Pass 2: ensure first non-system message is user
    system_msgs = [m for m in merged if m["role"] == "system"]
    conv_msgs   = [m for m in merged if m["role"] != "system"]
    if conv_msgs and conv_msgs[0]["role"] != "user":
        conv_msgs.insert(0, {"role": "user", "content": "（对话开始）"})

    return system_msgs + conv_msgs


class OpenAICompatProvider(LLMProvider):
    """Supports any OpenAI-compatible API: OpenAI, DeepSeek, Qwen, Gemini-compat, Ollama, etc."""

    def __init__(self, api_key: str = "", base_url: str = "", model: str = "", **kwargs):
        self.model = model or "gpt-4o-mini"
        # 默认 60 秒，避免网络/代理稍慢就 ConnectTimeout；可在 preset 里用 timeout 覆盖
        timeout = kwargs.get("timeout")
        if timeout is not None:
            timeout = float(timeout)
        else:
            timeout = 60.0
        self.client = AsyncOpenAI(
            api_key=api_key or "sk-placeholder",
            base_url=base_url or None,
            timeout=timeout,
        )
        unsupported = set(kwargs.get("unsupported_params", []))
        all_gen_kwargs: Dict[str, Any] = {
            "temperature": kwargs.get("temperature", 0.9),
            "top_p": kwargs.get("top_p", 0.95),
            "presence_penalty": kwargs.get("presence_penalty", 0.5),
            "frequency_penalty": kwargs.get("frequency_penalty", 0.4),
        }
        max_tokens = kwargs.get("max_tokens")
        if max_tokens is not None:
            all_gen_kwargs["max_tokens"] = int(max_tokens)
        self._gen_kwargs = {k: v for k, v in all_gen_kwargs.items() if k not in unsupported}
        self._extra_body = kwargs.get("extra_body") or None

    async def stream_chat(
        self, messages: List[Dict[str, str]], **kwargs
    ) -> AsyncGenerator[str, None]:
        merged = {**self._gen_kwargs, **kwargs}
        if self._extra_body:
            merged["extra_body"] = self._extra_body
        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=_normalize_messages(messages),
                stream=True,
                **merged,
            )
            async for chunk in stream:
                if not getattr(chunk, "choices", None) or not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice is None:
                    continue
                delta = getattr(choice, "delta", None)
                if delta is not None:
                    content = getattr(delta, "content", None)
                    if content:
                        yield content
        except Exception as e:
            err_msg = str(e)
            logger.error(f"[LLM] stream_chat error: {e}")
            log_error("llm", err_msg, {"model": self.model}, traceback_str=traceback.format_exc())
            log_debug("llm_stream_error", model=self.model, error=err_msg)
            yield f"[LLM Error: {e}]"

    async def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        merged = {**self._gen_kwargs, **kwargs}
        if self._extra_body:
            merged["extra_body"] = self._extra_body
        try:
            result = await self.client.chat.completions.create(
                model=self.model,
                messages=_normalize_messages(messages),
                stream=False,
                **merged,
            )
            if not getattr(result, "choices", None) or not result.choices:
                err = "API returned no choices"
                log_error("llm", err, {"model": self.model})
                log_debug("llm_chat_error", model=self.model, error=err)
                return f"[LLM Error: {err}]"
            choice = result.choices[0]
            message = getattr(choice, "message", None)
            if message is None:
                err = "API returned choice with no message"
                log_error("llm", err, {"model": self.model})
                log_debug("llm_chat_error", model=self.model, error=err)
                return f"[LLM Error: {err}]"
            content = getattr(message, "content", None)
            return (content or "") if content is not None else ""
        except Exception as e:
            err_msg = str(e)
            logger.error(f"[LLM] chat error: {e}")
            log_error("llm", err_msg, {"model": self.model}, traceback_str=traceback.format_exc())
            log_debug("llm_chat_error", model=self.model, error=err_msg)
            return f"[LLM Error: {e}]"
