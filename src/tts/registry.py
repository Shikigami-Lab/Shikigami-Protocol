import logging
from typing import Any, Dict

from src.tts.base import TTSProvider
from src.tts.edge_tts_provider import EdgeTTSProvider
from src.tts.gpt_sovits_provider import GptSoVitsProvider
from src.tts.qwen3_tts_provider import Qwen3TTSProvider
from src.tts.kokoro_provider import KokoroProvider

logger = logging.getLogger(__name__)

_REGISTRY: Dict[str, type] = {
    "edge_tts": EdgeTTSProvider,
    "gpt_sovits": GptSoVitsProvider,
    "qwen3_tts": Qwen3TTSProvider,
    "kokoro": KokoroProvider,
}


def get_tts_provider(config: Dict[str, Any]) -> TTSProvider:
    """Instantiate a TTSProvider from a config dict.

    For edge_tts: expects ``{"type": "edge_tts", "voice": "zh-CN-..."}``
    For gpt_sovits: expects ``{"type": "gpt_sovits", "host": ..., "port": ..., ...}``
    For qwen3_tts: expects ``{"type": "qwen3_tts", "mode": "custom_voice", "speaker": "Vivian", ...}``
    """
    tts_type = config.get("type", "edge_tts")
    cls = _REGISTRY.get(tts_type, EdgeTTSProvider)

    if tts_type == "gpt_sovits":
        return cls(
            host=config.get("host", "127.0.0.1"),
            port=int(config.get("port", 9880)),
            text_lang=config.get("text_lang", "zh"),
            prompt_text=config.get("prompt_text", ""),
            prompt_lang=config.get("prompt_lang", "zh"),
            ref_audio_path=config.get("ref_audio_path", ""),
            speed_factor=float(config.get("speed_factor", 1.0)),
            temperature=float(config.get("temperature", 1.0)),
            top_p=float(config.get("top_p", 1.0)),
            top_k=int(config.get("top_k", 15)),
            repetition_penalty=float(config.get("repetition_penalty", 1.35)),
        )

    if tts_type == "qwen3_tts":
        return cls(
            mode=config.get("mode", "custom_voice"),
            model_id=config.get("model_id", ""),
            device=config.get("device", "cuda:0"),
            dtype=config.get("dtype", "bfloat16"),
            attn_implementation=config.get("attn_implementation", "eager"),
            language=config.get("language", "Chinese"),
            speaker=config.get("speaker", "Vivian"),
            instruct=config.get("instruct", ""),
            voice_description=config.get("voice_description", ""),
            ref_audio_path=config.get("ref_audio_path", ""),
            ref_text=config.get("ref_text", ""),
            temperature=float(config.get("temperature", 0.9)),
            top_p=float(config.get("top_p", 1.0)),
            top_k=int(config.get("top_k", 50)),
            repetition_penalty=float(config.get("repetition_penalty", 1.05)),
            use_torch_compile=bool(config.get("use_torch_compile", False)),
            use_sentence_chunking=bool(config.get("use_sentence_chunking", False)),
            sentence_max_chars=int(config.get("sentence_max_chars", 0) or 0),
        )

    if tts_type == "kokoro":
        return cls(
            voice=config.get("voice", ""),
            lang=config.get("lang", "zh"),
            speed=float(config.get("speed", 1.0)),
            auto_detect_lang=bool(config.get("auto_detect_lang", True)),
        )

    # Default: edge_tts (and any unknown types)
    return cls(voice=config.get("voice", "zh-CN-XiaoxiaoNeural"))


def register_tts(type_name: str, cls: type) -> None:
    """Register a custom TTSProvider class under a type name."""
    _REGISTRY[type_name] = cls
    logger.info(f"[TTSRegistry] registered: {type_name}")
