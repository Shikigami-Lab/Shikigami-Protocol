"""Gemini 原生 Provider — 走 google-genai SDK 的原生 API（非 OpenAI 兼容端点）。

与 OpenAICompatProvider 的差异：
- `safety_settings` 原生支持（OpenAI 兼容端点会 400 拒收该字段）。
  preset 里可配 `safety_settings: [{category, threshold}, ...]`；
  不配置则不发送该字段，使用 Google 默认阈值。
- system 消息映射为原生 `system_instruction`。
- `google-genai` 懒加载：未安装时不影响其他 provider，实际调用才报错提示。

preset 示例（config/app.yaml 的 llm_presets 下）：
    Gemini:
      type: gemini
      api_key: ''
      model: gemini-2.5-flash
      temperature: 0.9
      top_p: 0.95
      max_tokens: 8192          # → max_output_tokens
      safety_settings:           # 可选；省略 = Google 默认
        - category: HARM_CATEGORY_HARASSMENT
          threshold: BLOCK_ONLY_HIGH
"""
import logging
import traceback
from typing import AsyncGenerator, Dict, List, Optional
from urllib.parse import urlsplit

from src.llm.base import LLMProvider
from src.llm.think_filter import ThinkTagFilter, strip_think_tags
from src.utils.debug_logger import log_error, log_debug

logger = logging.getLogger(__name__)

_IMPORT_HINT = "gemini_native provider 需要 google-genai 包：pip install google-genai"


def _lazy_import():
    """延迟导入 google-genai；未安装时给出可操作的错误信息。"""
    try:
        from google import genai
        from google.genai import types
        return genai, types
    except ImportError as e:  # pragma: no cover - 取决于部署环境
        raise RuntimeError(_IMPORT_HINT) from e


def _is_openai_compat_url(url: str) -> bool:
    """识别 OpenAI 兼容端点地址（路径以 /openai 结尾），如 …/v1beta/openai/。"""
    try:
        path = urlsplit(url).path.rstrip("/").lower()
    except ValueError:
        return False
    return path.endswith(("/openai", "/openai/chat/completions"))


def _normalize_native_base_url(base_url: str) -> Optional[str]:
    """Keep native proxies; only the official compatibility endpoint can default to Google."""
    raw_base = (base_url or "").strip()
    if not raw_base:
        return None
    parsed = urlsplit(raw_base)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password):
        raise ValueError("Configure an HTTP(S) native Gemini endpoint without URL credentials in Models settings")
    if _is_openai_compat_url(raw_base):
        if (parsed.scheme == "https" and parsed.hostname == "generativelanguage.googleapis.com"
                and parsed.port in (None, 443) and not parsed.query and not parsed.fragment
                and parsed.path.rstrip("/").lower() in (
                    "/v1beta/openai", "/v1beta/openai/chat/completions")):
            return None
        raise ValueError(
            "Gemini base_url is an OpenAI compatibility endpoint. In Models settings, "
            "set type to openai_compat to keep this gateway, or replace base_url with "
            "its native Gemini endpoint. The destination has not been changed."
        )
    return raw_base


def _split_system(messages: List[Dict[str, str]]):
    """OpenAI 风格消息 → (system_instruction, [(role, text), ...])。

    assistant → model；system 合并为 system_instruction；其余按 user 处理。
    """
    system_parts: List[str] = []
    convo: List = []
    for m in messages:
        role = (m.get("role") or "").lower()
        content = m.get("content") or ""
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        elif role == "assistant":
            convo.append(("model", content))
        else:
            convo.append(("user", content))
    return "\n\n".join(system_parts), convo


class GeminiNativeProvider(LLMProvider):
    """Google Gemini 原生 API provider（google-genai SDK）。"""

    def __init__(self, api_key: str = "", base_url: str = "", model: str = "", **kwargs):
        self.model = model or "gemini-2.5-flash"
        self.api_key = api_key or ""
        self._base_url = _normalize_native_base_url(base_url)
        self._unsupported = set(kwargs.get("unsupported_params") or [])
        timeout = kwargs.get("timeout")
        self._timeout = float(timeout) if timeout is not None else 60.0
        self._gen_kwargs: Dict = {
            "temperature": kwargs.get("temperature", 0.9),
            "top_p": kwargs.get("top_p", 0.95),
        }
        max_tokens = kwargs.get("max_tokens")
        if max_tokens is not None:
            self._gen_kwargs["max_output_tokens"] = int(max_tokens)
        # safety_settings: [{category, threshold}, ...]；None/[] = 不发送，用默认
        raw_ss = kwargs.get("safety_settings")
        self._safety_settings = [s for s in (raw_ss or []) if isinstance(s, dict)]
        self._client = None  # 首次调用时创建

    def _get_client(self):
        if self._client is None:
            genai, types = _lazy_import()
            http_options = types.HttpOptions(timeout=int(self._timeout * 1000))
            if self._base_url:
                http_options.base_url = self._base_url
            self._client = genai.Client(
                api_key=self.api_key or "missing-key",
                http_options=http_options,
            )
        return self._client

    def _build_config(self, types, system_instruction: str, merged_kwargs: Dict):
        cfg: Dict = {}
        for src_key in ("temperature", "top_p", "max_output_tokens"):
            v = merged_kwargs.get(src_key)
            if src_key == "max_output_tokens" and merged_kwargs.get("max_tokens") is not None:
                v = int(merged_kwargs["max_tokens"])
            if v is not None and src_key not in self._unsupported:
                cfg[src_key] = v
        if system_instruction:
            cfg["system_instruction"] = system_instruction
        if self._safety_settings:
            cfg["safety_settings"] = [
                types.SafetySetting(category=s["category"], threshold=s["threshold"])
                for s in self._safety_settings
                if s.get("category") and s.get("threshold")
            ]
        return types.GenerateContentConfig(**cfg)

    def _build_contents(self, types, messages: List[Dict[str, str]]):
        system_instruction, convo = _split_system(messages)
        contents = [
            types.Content(role=role, parts=[types.Part.from_text(text=text)])
            for role, text in convo
        ]
        return system_instruction, contents

    async def list_models(self) -> List[str]:
        client = self._get_client()
        names = []
        async for m in await client.aio.models.list():
            name = getattr(m, "name", None)
            if name:
                names.append(str(name).removeprefix("models/"))
        return sorted(names, key=str.casefold)

    async def stream_chat(
        self, messages: List[Dict[str, str]], **kwargs
    ) -> AsyncGenerator[str, None]:
        try:
            _, types = _lazy_import()
            client = self._get_client()
            merged = {**self._gen_kwargs, **kwargs}
            system_instruction, contents = self._build_contents(types, messages)
            config = self._build_config(types, system_instruction, merged)
            stream = await client.aio.models.generate_content_stream(
                model=self.model,
                contents=contents,
                config=config,
            )
            flt = ThinkTagFilter()
            async for chunk in stream:
                try:
                    text = chunk.text
                except (ValueError, AttributeError):
                    text = None  # 无文本 part（如安全拦截的候选）
                if text:
                    visible = flt.feed(text)
                    if visible:
                        yield visible
            tail = flt.flush()
            if tail:
                yield tail
        except Exception as e:
            err_msg = str(e)
            logger.error(f"[LLM] gemini_native stream_chat error: {e}")
            log_error("llm", err_msg, {"model": self.model}, traceback_str=traceback.format_exc())
            log_debug("llm_stream_error", model=self.model, error=err_msg)
            yield f"[LLM Error: {e}]"

    async def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        try:
            _, types = _lazy_import()
            client = self._get_client()
            merged = {**self._gen_kwargs, **kwargs}
            system_instruction, contents = self._build_contents(types, messages)
            config = self._build_config(types, system_instruction, merged)
            result = await client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
            try:
                content = result.text
            except (ValueError, AttributeError):
                content = None
            if not content:
                feedback = getattr(result, "prompt_feedback", None)
                block = getattr(feedback, "block_reason", None) if feedback else None
                err = f"empty response (block_reason={block})" if block else "empty response"
                log_error("llm", err, {"model": self.model})
                log_debug("llm_chat_error", model=self.model, error=err)
                return f"[LLM Error: {err}]"
            return strip_think_tags(content)
        except Exception as e:
            err_msg = str(e)
            logger.error(f"[LLM] gemini_native chat error: {e}")
            log_error("llm", err_msg, {"model": self.model}, traceback_str=traceback.format_exc())
            log_debug("llm_chat_error", model=self.model, error=err_msg)
            return f"[LLM Error: {e}]"
