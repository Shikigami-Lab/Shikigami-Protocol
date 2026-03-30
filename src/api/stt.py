"""
STT API — 服务端语音识别（SenseVoice / sherpa-onnx）

POST /stt：接收 WAV 音频（multipart/form-data 字段 "audio" 或 application/octet-stream），
转写后返回 { "text": "..." }；错误时 { "error": "..." }。

前端通过 _blobToWav() 将录音转为 16kHz mono WAV 后发送。
"""

import asyncio
import logging
import os
import re
import tempfile
from typing import Optional

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()

# ── sherpa-onnx SenseVoice 单例 ───────────────────────────────────────────────
_sherpa_recognizer = None
_sherpa_recognizer_config = None

_SHERPA_SENSE_VOICE_SEARCH_DIRS = [
    "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue",
]

# SenseVoice 情绪/语言标签，如 <|HAPPY|><|zh|> — 转写后去掉
_SENSE_VOICE_TAG_RE = re.compile(r"<\|[^|]+\|>")

# SenseVoice 对静默/噪声常产生的幻觉字符串（单个韩文字等）
_NOISE_HALLUCINATIONS = frozenset(["그", "그.", "그리", "네", "あ", "え", "ん"])


def _resolve_sherpa_model_dir(cfg_model_path: str) -> str:
    """返回可用的 sherpa-onnx SenseVoice 模型目录（绝对路径），未找到返回空串。"""
    if cfg_model_path and os.path.isdir(cfg_model_path):
        return os.path.abspath(cfg_model_path)
    cwd = os.getcwd()
    for rel in _SHERPA_SENSE_VOICE_SEARCH_DIRS:
        fp = os.path.join(cwd, rel)
        if os.path.isdir(fp):
            return fp
    return ""


def _get_sherpa_recognizer(request: Request):
    """懒加载 sherpa-onnx SenseVoice；配置变更时重新加载。"""
    global _sherpa_recognizer, _sherpa_recognizer_config
    config = request.app.state.config
    stt_cfg = config.get_stt_config()
    if not stt_cfg.get("enabled"):
        return None

    cfg_path = (stt_cfg.get("model_path") or "").strip()
    model_dir = _resolve_sherpa_model_dir(cfg_path)
    if not model_dir:
        logger.warning("[STT] sherpa-onnx SenseVoice 模型目录未找到，请下载到 models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue")
        return None

    num_threads = int(stt_cfg.get("num_threads", 4))
    current_key = (model_dir, num_threads)
    if _sherpa_recognizer is not None and _sherpa_recognizer_config == current_key:
        return _sherpa_recognizer

    try:
        import sherpa_onnx
    except ImportError:
        logger.warning("[STT] sherpa_onnx 未安装，请 pip install sherpa-onnx")
        return None

    # 优先 INT8 量化模型，回退到 FP32
    model_file = os.path.join(model_dir, "model.int8.onnx")
    if not os.path.isfile(model_file):
        model_file = os.path.join(model_dir, "model.onnx")
    tokens_file = os.path.join(model_dir, "tokens.txt")

    if not os.path.isfile(model_file):
        logger.warning("[STT] 未找到 model.int8.onnx / model.onnx: %s", model_dir)
        return None
    if not os.path.isfile(tokens_file):
        logger.warning("[STT] 未找到 tokens.txt: %s", model_dir)
        return None

    try:
        _sherpa_recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model_file,
            tokens=tokens_file,
            use_itn=True,
            num_threads=num_threads,
            debug=False,
        )
        _sherpa_recognizer_config = current_key
        logger.info("[STT] sherpa-onnx SenseVoice 已加载: %s (threads=%d)", model_dir, num_threads)
        return _sherpa_recognizer
    except Exception as e:
        logger.exception("[STT] 加载 sherpa-onnx 失败: %s", e)
        return None


def _transcribe_sherpa_sync(audio_path: str, recognizer) -> str:
    """
    执行 sherpa-onnx SenseVoice 转写。
    输入必须是 WAV（前端 _blobToWav() 已转换）。
    使用 soundfile 读取 PCM，无需 ffmpeg。
    """
    import numpy as np

    # 读取 WAV → float32 PCM
    try:
        import soundfile as sf
        samples, sample_rate = sf.read(audio_path, dtype="float32", always_2d=False)
    except Exception:
        # soundfile 读取失败时用 scipy 兜底（仅 WAV）
        import scipy.io.wavfile as wf
        sample_rate, data = wf.read(audio_path)
        samples = data.astype(np.float32) / (32768.0 if data.dtype == np.int16 else 1.0)

    # SenseVoice 要求 16kHz mono
    if samples.ndim > 1:
        samples = samples[:, 0]  # 取第一声道
    if sample_rate != 16000:
        ratio = 16000 / sample_rate
        new_len = int(len(samples) * ratio)
        samples = np.interp(
            np.linspace(0, len(samples) - 1, new_len),
            np.arange(len(samples)),
            samples,
        ).astype(np.float32)
        sample_rate = 16000

    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate, samples)
    recognizer.decode_stream(stream)
    text = _SENSE_VOICE_TAG_RE.sub("", stream.result.text).strip()
    return text


def _filter_noise(text: str, min_chars: int) -> str:
    """过滤 SenseVoice 对静默/噪声产生的幻觉输出。"""
    if not text:
        return ""
    # 已知幻觉字符串黑名单
    if text in _NOISE_HALLUCINATIONS:
        return ""
    # 最小字符数过滤（去标点后计算）
    if min_chars > 0:
        clean = re.sub(r"[\s\W]", "", text)
        if len(clean) < min_chars:
            return ""
    return text


# ── 端点 ──────────────────────────────────────────────────────────────────────

@router.get("/stt/config")
async def stt_config(request: Request):
    """返回 STT 配置，供前端显示/隐藏麦克风按钮。"""
    stt_cfg = request.app.state.config.get_stt_config()
    return {
        "enabled": stt_cfg.get("enabled", False),
        "language": stt_cfg.get("language", "zh"),
    }


@router.post("/stt")
async def stt_transcribe(
    request: Request,
    audio: Optional[UploadFile] = File(None),
):
    """
    接收 WAV 音频并用 SenseVoice 转写为文字。
    请求：multipart/form-data 字段 "audio" 或 body 为 application/octet-stream。
    响应：{ "text": "识别结果" } 或 { "error": "..." }。
    """
    config = request.app.state.config
    stt_cfg = config.get_stt_config()
    if not stt_cfg.get("enabled"):
        return JSONResponse(
            status_code=503,
            content={"error": "STT 未启用，请在 config/app.yaml 中设置 stt.enabled: true"},
        )

    body = None
    if audio:
        body = await audio.read()
    if not body:
        body = await request.body()
    if not body:
        return JSONResponse(status_code=400, content={"error": "未收到音频数据"})

    recognizer = _get_sherpa_recognizer(request)
    if recognizer is None:
        return JSONResponse(
            status_code=503,
            content={"error": "SenseVoice 模型不可用，请下载模型到 models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue"},
        )

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
            f.write(body)
            tmp_path = f.name

        min_chars = int(stt_cfg.get("min_chars", 2))
        text = await asyncio.to_thread(_transcribe_sherpa_sync, tmp_path, recognizer)
        text = _filter_noise(text, min_chars)
        return {"text": text or ""}
    except Exception as e:
        logger.exception("[STT] 转写失败: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
