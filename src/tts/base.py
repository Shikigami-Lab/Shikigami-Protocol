from abc import ABC, abstractmethod
from typing import Any


class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str, **kwargs: Any) -> bytes:
        """Convert text to audio bytes (MP3 or WAV). kwargs 可选，如 instruct_override 供 Qwen3-TTS 情绪等扩展。"""
        ...
