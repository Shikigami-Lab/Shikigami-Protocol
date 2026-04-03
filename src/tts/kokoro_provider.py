"""
KokoroTTS provider — 本地 ONNX TTS，支持中文（misaki[zh]）、日语（misaki[ja]）、英语。

安装：
    pip install kokoro-onnx
    pip install misaki[zh]   # 中文 G2P
    pip install misaki[ja]   # 日语 G2P（需系统有 espeak-ng）

模型文件（首次运行自动下载到 ~/.cache/kokoro/，共约 300 MB）：
    kokoro-v0_19.onnx
    voices-v0_19.bin

Config 示例（config/app.yaml tts.kokoro）：
    voice: "zf_xiaobei"   # 中文女 / zh: zf_xiaobei zf_xiaoni, ja: jf_alpha, en: af_sarah
    lang: "zh"            # zh | ja | en-us
    speed: 1.0
"""

import asyncio
import io
import logging
import os
from typing import Any, Optional

from src.tts.base import TTSProvider
from src.utils.paths import get_project_root

logger = logging.getLogger(__name__)

# 每种语言的默认音色
_DEFAULT_VOICE: dict = {
    "zh": "zf_xiaobei",
    "ja": "jf_alpha",
    "en-us": "af_sarah",
    "en-gb": "bf_emma",
}

_kokoro_instance: Optional[Any] = None
_kokoro_import_warned = False

# Model file search order: project models/ dir, then project root, then cwd
_PROJECT_ROOT = get_project_root()
_MODEL_SEARCH_DIRS = [
    os.path.join(_PROJECT_ROOT, "models"),
    _PROJECT_ROOT,
    os.getcwd(),
]
# New (v1.0) filenames take priority; fall back to old (v0_19) if not found
_MODEL_CANDIDATES = ["kokoro-v1.0.onnx", "kokoro-v0_19.onnx"]
_VOICES_CANDIDATES = ["voices-v1.0.bin", "voices-v0_19.bin"]


def _find_model_files() -> Optional[tuple]:
    """Search common directories for kokoro model + voices files. Returns (model_path, voices_path) or None."""
    for d in _MODEL_SEARCH_DIRS:
        for mf in _MODEL_CANDIDATES:
            model_path = os.path.join(d, mf)
            if not os.path.isfile(model_path):
                continue
            for vf in _VOICES_CANDIDATES:
                voices_path = os.path.join(d, vf)
                if os.path.isfile(voices_path):
                    return model_path, voices_path
    return None


def _get_kokoro():
    global _kokoro_instance, _kokoro_import_warned  # noqa: PLW0603
    if _kokoro_instance is not None:
        return _kokoro_instance
    try:
        from kokoro_onnx import Kokoro
    except ImportError:
        if not _kokoro_import_warned:
            _kokoro_import_warned = True
            logger.warning("[KokoroTTS] kokoro-onnx 未安装，请执行: pip install kokoro-onnx")
        return None

    found = _find_model_files()
    if found is None:
        if not _kokoro_import_warned:
            _kokoro_import_warned = True
            logger.warning(
                "[KokoroTTS] 未找到模型文件 (kokoro-v1.0.onnx + voices-v1.0.bin)，"
                "请下载并放置到 %s/models/ 目录下。"
                "下载地址: https://github.com/thewh1teagle/kokoro-onnx/releases",
                _PROJECT_ROOT,
            )
        return None

    model_path, voices_path = found
    try:
        _kokoro_instance = Kokoro(model_path, voices_path)
        logger.info("[KokoroTTS] 模型已加载: %s | %s", model_path, voices_path)
        return _kokoro_instance
    except Exception as e:
        logger.warning("[KokoroTTS] 模型加载失败: %s", e)
        return None


def _detect_lang(text: str) -> str:
    """简单语言探测：中文字符优先判中文，片假名/平假名判日语，其余英语。"""
    zh_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    ja_count = sum(1 for c in text if "\u3040" <= c <= "\u30ff")
    if zh_count > ja_count and zh_count > 0:
        return "zh"
    if ja_count > 0:
        return "ja"
    return "en-us"


def _get_phonemes(text: str, lang: str) -> Optional[str]:
    """将文本转为 phoneme 串（中文/日文需要 G2P；英文直接返回 None 让 kokoro 自处理）。"""
    if lang == "zh":
        try:
            from misaki import zh
            # misaki >= 0.9: ZHG2P; older versions may export G2P — try both
            g2p_cls = getattr(zh, "ZHG2P", None) or getattr(zh, "G2P", None)
            if g2p_cls is None:
                raise ImportError("misaki.zh has no ZHG2P or G2P")
            g2p = g2p_cls()
            phonemes, _ = g2p(text)
            return phonemes
        except ImportError:
            logger.warning("[KokoroTTS] misaki[zh] 未安装，请执行: pip install misaki[zh]")
            return None
        except Exception as e:
            logger.warning("[KokoroTTS] zh G2P 失败: %s", e)
            return None

    if lang == "ja":
        try:
            from misaki import ja
            g2p = ja.MisakiG2P()
            phonemes, _ = g2p(text)
            return phonemes
        except ImportError:
            logger.warning("[KokoroTTS] misaki[ja] 未安装，请执行: pip install misaki[ja]")
            return None
        except Exception as e:
            logger.debug("[KokoroTTS] ja G2P 失败: %s", e)
            return None

    # 英语：kokoro 自带 G2P，传原文即可
    return None


def _synthesize_sync(text: str, voice: str, lang: str, speed: float) -> bytes:
    """同步合成，在线程中运行。"""
    kokoro = _get_kokoro()
    if kokoro is None:
        return b""

    # G2P — for zh/ja we produce IPA phonemes and pass is_phonemes=True;
    # for en (or when G2P fails) let kokoro handle the raw text directly.
    phonemes = _get_phonemes(text, lang)
    if phonemes is not None:
        input_text = phonemes
        is_phonemes = True
        create_lang = "en-us"   # lang arg is irrelevant when is_phonemes=True
    else:
        input_text = text
        is_phonemes = False
        create_lang = lang if lang in ("en-us", "en-gb") else "en-us"

    try:
        import soundfile as sf
        samples, sr = kokoro.create(input_text, voice=voice, speed=speed,
                                    lang=create_lang, is_phonemes=is_phonemes)
        buf = io.BytesIO()
        sf.write(buf, samples, sr, format="WAV")
        return buf.getvalue()
    except ImportError:
        logger.warning("[KokoroTTS] soundfile 未安装: pip install soundfile")
        return b""
    except Exception as e:
        logger.warning("[KokoroTTS] 合成失败: %s", e)
        return b""


class KokoroProvider(TTSProvider):
    """KokoroTTS 本地 ONNX provider — 低延迟，支持中/日/英。"""

    def __init__(
        self,
        voice: str = "",
        lang: str = "zh",
        speed: float = 1.0,
        auto_detect_lang: bool = True,
        **kwargs,
    ):
        self.lang = (lang or "zh").strip().lower() or "zh"
        self.speed = max(0.5, min(2.0, float(speed or 1.0)))
        self.auto_detect_lang = bool(auto_detect_lang)
        # 如果未指定音色，按 lang 选默认
        default = _DEFAULT_VOICE.get(self.lang, "af_sarah")
        self.voice = (voice or default).strip() or default

    async def synthesize(self, text: str, **kwargs: Any) -> bytes:
        lang = self.lang
        voice = self.voice

        # 自动语言探测：若 lang 为 auto 或文本语言与配置不符
        if self.auto_detect_lang:
            detected = _detect_lang(text)
            if detected != lang:
                lang = detected
                voice = _DEFAULT_VOICE.get(lang, voice)

        return await asyncio.to_thread(_synthesize_sync, text, voice, lang, self.speed)
