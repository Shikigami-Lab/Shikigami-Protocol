import logging

import httpx

from src.tts.base import TTSProvider

logger = logging.getLogger(__name__)


class GptSoVitsProvider(TTSProvider):
    """GPT-SoVITS local TTS provider.

    Calls the GPT-SoVITS HTTP API (default: http://127.0.0.1:9880/tts).
    Per-character voice samples (ref_text / ref_audio_path) are injected
    by ws_tts.py from the active session's profile card.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9880,
        text_lang: str = "zh",
        prompt_text: str = "",
        prompt_lang: str = "zh",
        ref_audio_path: str = "",
        speed_factor: float = 1.0,
        temperature: float = 1.0,
        top_p: float = 1.0,
        top_k: int = 15,
        repetition_penalty: float = 1.35,
        **kwargs,
    ):
        self.base_url = f"http://{host}:{port}"
        self.text_lang = text_lang
        self.prompt_text = prompt_text
        self.prompt_lang = prompt_lang
        self.ref_audio_path = ref_audio_path
        self.speed_factor = speed_factor
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.repetition_penalty = repetition_penalty

    async def synthesize(self, text: str, **kwargs) -> bytes:
        payload = {
            "text": text,
            "text_lang": self.text_lang,
            "speed_factor": self.speed_factor,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "repetition_penalty": self.repetition_penalty,
        }
        if self.prompt_text:
            payload["prompt_text"] = self.prompt_text
            payload["prompt_lang"] = self.prompt_lang
        if self.ref_audio_path:
            payload["ref_audio_path"] = self.ref_audio_path
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(f"{self.base_url}/tts", json=payload)
                r.raise_for_status()
                return r.content
        except Exception as e:
            logger.warning(f"[GptSoVits] synthesize failed: {e}")
            return b""
