import logging
from src.tts.base import TTSProvider

logger = logging.getLogger(__name__)


class EdgeTTSProvider(TTSProvider):
    """Microsoft Edge TTS — cloud, free, no API key needed. Returns MP3 bytes."""

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural", **kwargs):
        self.voice = voice

    async def synthesize(self, text: str, **kwargs) -> bytes:
        try:
            import edge_tts
            communicate = edge_tts.Communicate(text, self.voice)
            audio_data = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data += chunk["data"]
            return audio_data
        except Exception as e:
            logger.warning(f"[EdgeTTS] synthesis failed: {e}")
            return b""
