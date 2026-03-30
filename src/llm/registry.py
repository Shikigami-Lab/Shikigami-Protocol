import logging
from typing import Any, Dict, Optional

from src.llm.base import LLMProvider
from src.llm.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)

# Map type string → provider class
_REGISTRY: Dict[str, type] = {
    "openai_compat": OpenAICompatProvider,
    "openai": OpenAICompatProvider,
    "ollama": OpenAICompatProvider,
    "lmstudio": OpenAICompatProvider,
    "deepseek": OpenAICompatProvider,
    "gemini": OpenAICompatProvider,
}


def get_provider(preset: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> LLMProvider:
    """Instantiate an LLMProvider from a preset config dict.

    If *overrides* is provided, its non-None values take precedence over the
    preset for generation params (temperature, top_p, presence_penalty,
    frequency_penalty, max_tokens).  Used by secondary models so each role can
    tune its own sampling params independently from the shared preset.
    """
    merged = dict(preset)
    if overrides:
        for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "max_tokens"):
            v = overrides.get(k)
            if v is not None:
                merged[k] = v
    provider_type = merged.get("type", "openai_compat")
    cls = _REGISTRY.get(provider_type, OpenAICompatProvider)
    return cls(
        api_key=merged.get("api_key", ""),
        base_url=merged.get("base_url", ""),
        model=merged.get("model", ""),
        temperature=merged.get("temperature", 0.9),
        top_p=merged.get("top_p", 0.95),
        presence_penalty=merged.get("presence_penalty", 0.5),
        frequency_penalty=merged.get("frequency_penalty", 0.4),
        max_tokens=merged.get("max_tokens"),
        unsupported_params=merged.get("unsupported_params", []),
        extra_body=merged.get("extra_body"),
        timeout=merged.get("timeout"),  # 可选，秒；不设则默认 60
    )


def register_provider(type_name: str, cls: type) -> None:
    """Register a custom LLMProvider class under a type name."""
    _REGISTRY[type_name] = cls
    logger.info(f"[LLMRegistry] registered provider: {type_name}")


# ── 角色 provider（辅助模型） ─────────────────────────────────────────────────
_ROLE_PROVIDERS: Dict[str, LLMProvider] = {}


def register_role_provider(role: str, provider: LLMProvider) -> None:
    """按角色名注册一个 provider 实例（如 'analysis'）。"""
    _ROLE_PROVIDERS[role] = provider
    logger.info("[LLMRegistry] registered role provider: %s", role)


def unregister_role_provider(role: str) -> None:
    """移除角色 provider（如用户禁用辅助模型时调用）。"""
    if role in _ROLE_PROVIDERS:
        del _ROLE_PROVIDERS[role]
        logger.info("[LLMRegistry] unregistered role provider: %s", role)


def ensure_analysis_provider(config: Any) -> None:
    """若配置了 analysis 辅助模型，则注册为 role provider 'analysis'。"""
    sec = config.secondary_models.get("analysis", {}) if hasattr(config, "secondary_models") else {}
    if not sec.get("enabled"):
        return
    preset_name = sec.get("preset", "")
    preset = config.llm_presets.get(preset_name) if (hasattr(config, "llm_presets") and preset_name) else None
    if not preset:
        return
    overrides = {k: sec.get(k) for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "max_tokens")}
    register_role_provider("analysis", get_provider(preset, overrides))


def get_provider_for_role(
    role: str,
    fallback: Optional[Dict[str, Any]] = None,
) -> Optional[LLMProvider]:
    """按角色查 provider；未注册则用 fallback preset 实例化；都无则返回 None（不 raise）。"""
    if role in _ROLE_PROVIDERS:
        return _ROLE_PROVIDERS[role]
    if fallback:
        return get_provider(fallback)
    return None
