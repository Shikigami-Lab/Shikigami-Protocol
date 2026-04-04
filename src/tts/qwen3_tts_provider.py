"""
Qwen3-TTS provider — 进程内调用 qwen-tts（CustomVoice / VoiceDesign / VoiceClone）。

支持三种模式；默认参数对齐 Qwen 官方（temperature=0.9, top_p=1.0, top_k=50, repetition_penalty=1.05）。
默认女性音色：Vivian（明亮年轻女声，中文）。
"""
import asyncio
import io
import logging
import os
import pathlib
import warnings
from typing import Any, Dict, List, Optional, Union

# 在任何可能触发 sox/torchaudio 的 import 之前先设好过滤器
warnings.filterwarnings("ignore", message=".*SoX could not be found.*")
warnings.filterwarnings("ignore", message=".*sox.*not.*found.*")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

from src.tts.base import TTSProvider
from src.tts.sentence_split import split_sentences_for_tts
from src.utils.paths import get_project_root

logger = logging.getLogger(__name__)

_DEFAULT_MODEL_IDS = {
    "custom_voice": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "voice_design": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "voice_clone": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
}
# 本地目录默认（离线优先）：相对项目根，与 huggingface-cli download --local-dir 一致
_LOCAL_MODEL_DIRS = {
    "custom_voice": "models/Qwen-Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "voice_design": "models/Qwen-Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "voice_clone": "models/Qwen-Qwen3-TTS-12Hz-1.7B-Base",
}
_HF_REPO_TO_LOCAL = {
    "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice": _LOCAL_MODEL_DIRS["custom_voice"],
    "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign": _LOCAL_MODEL_DIRS["voice_design"],
    "Qwen/Qwen3-TTS-12Hz-1.7B-Base": _LOCAL_MODEL_DIRS["voice_clone"],
}
_PROJECT_ROOT = get_project_root()


def _resolve_model_path(model_id) -> Union[pathlib.Path, str]:
    """相对路径按项目根解析为 Path；HF repo 名（Qwen/xxx）保持字符串。
    本地路径返回 pathlib.Path，绕过 huggingface_hub.validate_repo_id 对 Windows 路径的拒绝。
    """
    if isinstance(model_id, pathlib.Path):
        return model_id  # already resolved
    if not (model_id and model_id.strip()):
        return model_id or ""
    s = model_id.strip()
    if os.path.isabs(s):
        return pathlib.Path(s)
    # Hugging Face repo id 形如 Qwen/ModelName，不当作相对路径
    if s.startswith("Qwen/"):
        return s
    return pathlib.Path(os.path.normpath(os.path.join(_PROJECT_ROOT, s)))

# CustomVoice 内置音色，默认女性：Vivian
SUPPORTED_SPEAKERS = [
    "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric",
    "Ryan", "Aiden", "Ono_Anna", "Sohee",
]

_qwen_model: Optional[Any] = None
_qwen_model_key: Optional[tuple] = None
_qwen_import_warned = False
_matmul_precision_set = False
_qwen_failed_keys: set = set()   # keys that already failed; don't retry on every TTS call


def _suppress_noisy_warnings() -> None:
    """Suppress harmless third-party warnings that pollute the log on Windows."""
    import warnings
    # sox Python package: SoX executable not in PATH — we use soundfile for I/O anyway
    warnings.filterwarnings("ignore", message=".*SoX could not be found.*")
    warnings.filterwarnings("ignore", message=".*sox.*not.*found.*")
    # torchaudio: redirect it to soundfile backend so it never tries sox at all
    try:
        import torchaudio
        torchaudio.set_audio_backend("soundfile")
    except Exception:
        try:
            import torchaudio
            torchaudio.set_audio_backend("scipy")
        except Exception:
            pass


def _safe_device(device: str) -> str:
    """若请求 CUDA 但环境不支持，自动 fallback 到 cpu 并打印一次警告。"""
    if device == "cpu":
        return device
    try:
        import torch
        if not torch.cuda.is_available():
            logger.warning(
                "[Qwen3-TTS] CUDA 不可用（当前 PyTorch 为 CPU-only 版本），已自动切换到 cpu。"
                "若需 GPU 加速，请重新安装带 CUDA 的 PyTorch："
                " pip install torch --index-url https://download.pytorch.org/whl/cu124"
            )
            return "cpu"
    except ImportError:
        pass
    return device


def _get_model(model_id: str, device: str, dtype_name: str, attn: str, use_torch_compile: bool = False):
    global _qwen_model, _qwen_model_key, _qwen_import_warned, _matmul_precision_set, _qwen_failed_keys  # noqa: PLW0603
    device = _safe_device(device)
    key = (model_id, device, dtype_name, attn, use_torch_compile)
    if _qwen_model is not None and _qwen_model_key == key:
        return _qwen_model
    # Don't retry a combination that already failed this session
    if key in _qwen_failed_keys:
        return None
    _suppress_noisy_warnings()
    try:
        import torch
        from qwen_tts import Qwen3TTSModel
        # 一次设置：Ampere+ GPU 上加速 float32 矩阵乘（不依赖 flash-attn）
        if not _matmul_precision_set and device != "cpu":
            try:
                torch.set_float32_matmul_precision("high")
                _matmul_precision_set = True
                logger.info("[Qwen3-TTS] 已设置 torch float32 matmul precision=high")
            except Exception:
                pass
    except ImportError as e:
        if not _qwen_import_warned:
            _qwen_import_warned = True
            logger.warning(
                "[Qwen3-TTS] qwen-tts 未安装，请执行: pip install qwen-tts soundfile；错误: %s",
                e,
            )
        return None

    dtype = torch.bfloat16
    if dtype_name and "float32" in dtype_name.lower():
        dtype = torch.float32
    elif dtype_name and "float16" in dtype_name.lower():
        dtype = torch.float16

    attn_impl = "flash_attention_2" if (attn == "flash_attention_2") else "eager"
    if device == "cpu" and dtype == torch.bfloat16:
        dtype = torch.float32

    load_path = _resolve_model_path(model_id)
    # For GPU: use dict device_map to bypass accelerate's meta-device dispatch path.
    # For CPU: pass device_map=None — model defaults to CPU, no accelerate required.
    #   Passing device_map="cpu" (string) triggers the accelerate import check in
    #   transformers ≥4.38 even for CPU, causing ImportError when accelerate is absent.
    device_map_arg: Any = {"": device} if device != "cpu" else None
    fp_kwargs: dict = {"dtype": dtype, "attn_implementation": attn_impl}
    if device_map_arg is not None:
        fp_kwargs["device_map"] = device_map_arg
    try:
        _qwen_model = Qwen3TTSModel.from_pretrained(load_path, **fp_kwargs)
        if use_torch_compile and device != "cpu":
            try:
                _qwen_model = torch.compile(_qwen_model, mode="reduce-overhead")
                logger.info("[Qwen3-TTS] 已对模型启用 torch.compile(mode=reduce-overhead)，首句会稍慢，后续加速")
            except Exception as e:
                logger.warning("[Qwen3-TTS] torch.compile 失败，将不编译: %s", e)
        _qwen_model_key = key
        logger.info("[Qwen3-TTS] 模型已加载: %s device=%s", load_path, device)
        return _qwen_model
    except AttributeError as e:
        _qwen_failed_keys.add(key)
        if "endswith" in str(e) or "NoneType" in str(e):
            logger.warning(
                "[Qwen3-TTS] 模型未找到或未下载 (model_id=%s)。若已设置 HF_HUB_OFFLINE=1，请先取消离线并联网运行一次以自动下载，或执行: huggingface-cli download %s --local-dir models/%s；错误: %s",
                model_id,
                model_id,
                model_id.replace("/", "-"),
                e,
            )
        else:
            logger.exception("[Qwen3-TTS] 加载失败: %s", e)
        return None
    except Exception as e:
        _qwen_failed_keys.add(key)
        logger.exception("[Qwen3-TTS] 加载失败: %s", e)
        return None


def _synthesize_sync(
    text: str,
    mode: str,
    model_id: str,
    device: str,
    dtype: str,
    attn: str,
    language: str,
    speaker: str,
    instruct: str,
    voice_description: str,
    ref_audio_path: str,
    ref_text: str,
    temperature: Optional[float],
    top_p: Optional[float],
    top_k: Optional[int],
    repetition_penalty: Optional[float],
    use_torch_compile: bool = False,
) -> bytes:
    """在线程中执行合成，返回 WAV 字节。"""
    resolved_id = (model_id or "").strip() or _DEFAULT_MODEL_IDS.get(mode, _DEFAULT_MODEL_IDS["custom_voice"])
    # 选「语音克隆」时若当前仍是 CustomVoice 模型路径，自动改用 Base（优先本地目录，离线可用）
    if mode == "voice_clone" and ("CustomVoice" in resolved_id or "custom_voice" in resolved_id.lower()):
        local_base = _LOCAL_MODEL_DIRS["voice_clone"]
        if os.path.isdir(os.path.join(_PROJECT_ROOT, local_base)):
            resolved_id = local_base
        else:
            resolved_id = _DEFAULT_MODEL_IDS["voice_clone"]
    # 若 config 里仍是 HF repo 名且本地已有对应目录，优先用本地（离线）
    if resolved_id in _HF_REPO_TO_LOCAL:
        local_dir = _HF_REPO_TO_LOCAL[resolved_id]
        if os.path.isdir(os.path.join(_PROJECT_ROOT, local_dir)):
            resolved_id = local_dir
    resolved_id = _resolve_model_path(resolved_id)
    model = _get_model(resolved_id, device, dtype, attn, use_torch_compile)
    if model is None:
        return b""

    lang = (language or "Chinese").strip() or "Auto"
    gen_kw: Dict[str, Any] = {}
    if temperature is not None:
        gen_kw["temperature"] = temperature
    if top_p is not None:
        gen_kw["top_p"] = top_p
    if top_k is not None:
        gen_kw["top_k"] = top_k
    if repetition_penalty is not None:
        gen_kw["repetition_penalty"] = repetition_penalty

    try:
        import soundfile as sf
    except ImportError:
        logger.warning("[Qwen3-TTS] 需要 soundfile: pip install soundfile")
        return b""

    try:
        if mode == "custom_voice":
            spk = (speaker or "Vivian").strip() or "Vivian"
            inst = (instruct or "").strip() or None
            wavs, sr = model.generate_custom_voice(
                text=text,
                language=lang,
                speaker=spk,
                instruct=inst,
                **gen_kw,
            )
        elif mode == "voice_design":
            desc = (voice_description or instruct or "").strip()
            wavs, sr = model.generate_voice_design(
                text=text,
                language=lang,
                instruct=desc,
                **gen_kw,
            )
        elif mode == "voice_clone":
            ref_audio = (ref_audio_path or "").strip()
            if not ref_audio or not os.path.isfile(ref_audio):
                logger.warning("[Qwen3-TTS] voice_clone 需要有效 ref_audio_path")
                return b""
            ref_txt = (ref_text or "").strip() or None
            wavs, sr = model.generate_voice_clone(
                text=text,
                language=lang,
                ref_audio=ref_audio,
                ref_text=ref_txt,
                **gen_kw,
            )
        else:
            logger.warning("[Qwen3-TTS] 未知 mode=%s，使用 custom_voice", mode)
            wavs, sr = model.generate_custom_voice(
                text=text,
                language=lang,
                speaker=(speaker or "Vivian").strip() or "Vivian",
                instruct=(instruct or "").strip() or None,
                **gen_kw,
            )

        buf = io.BytesIO()
        sf.write(buf, wavs[0], sr, format="WAV")
        return buf.getvalue()
    except Exception as e:
        logger.exception("[Qwen3-TTS] 合成失败: %s", e)
        return b""


class Qwen3TTSProvider(TTSProvider):
    """Qwen3-TTS 进程内 Provider；支持 CustomVoice / VoiceDesign / VoiceClone。"""

    def __init__(
        self,
        mode: str = "custom_voice",
        model_id: str = "",
        device: str = "cuda:0",
        dtype: str = "bfloat16",
        attn_implementation: str = "eager",
        language: str = "Chinese",
        speaker: str = "Vivian",
        instruct: str = "",
        voice_description: str = "",
        ref_audio_path: str = "",
        ref_text: str = "",
        temperature: float = 0.9,
        top_p: float = 1.0,
        top_k: int = 50,
        repetition_penalty: float = 1.05,
        use_torch_compile: bool = False,
        use_sentence_chunking: bool = False,
        sentence_max_chars: int = 0,
        **kwargs,
    ):
        self.mode = (mode or "custom_voice").strip() or "custom_voice"
        self.use_torch_compile = bool(use_torch_compile)
        self.use_sentence_chunking = bool(use_sentence_chunking)
        self.sentence_max_chars = max(0, int(sentence_max_chars)) if sentence_max_chars else 0
        self.model_id = (model_id or "").strip()
        self.device = (device or "cuda:0").strip() or "cuda:0"
        self.dtype = (dtype or "bfloat16").strip() or "bfloat16"
        self.attn = (attn_implementation or "eager").strip() or "eager"
        self.language = (language or "Chinese").strip() or "Chinese"
        self.speaker = (speaker or "Vivian").strip() or "Vivian"
        self.instruct = (instruct or "").strip()
        self.voice_description = (voice_description or "").strip()
        self.ref_audio_path = (ref_audio_path or "").strip()
        self.ref_text = (ref_text or "").strip()
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.repetition_penalty = repetition_penalty

    async def synthesize(self, text: str, **kwargs) -> bytes:
        """合成语音。kwargs 可选 instruct_override：与 self.instruct 合并后的最终 instruct（如情绪→instruct）。"""
        instruct_override = (kwargs.get("instruct_override") or "").strip()
        final_instruct = f"{self.instruct}。{instruct_override}".strip("。").strip() if instruct_override else self.instruct

        # 前端 sendToTTS() 已在 WebSocket 层按句切分后逐句发送，后端再切纯属冗余。
        # 若输入本身已是短句（≤ sentence_max_chars 或 ≤ 40 字），直接跳过内部 chunking。
        _threshold = self.sentence_max_chars if self.sentence_max_chars > 0 else 40
        effective_chunking = self.use_sentence_chunking and len(text) > _threshold

        if not effective_chunking:
            return await asyncio.to_thread(
                _synthesize_sync,
                text,
                self.mode,
                self.model_id,
                self.device,
                self.dtype,
                self.attn,
                self.language,
                self.speaker,
                final_instruct,
                self.voice_description,
                self.ref_audio_path,
                self.ref_text,
                self.temperature,
                self.top_p,
                self.top_k,
                self.repetition_penalty,
                self.use_torch_compile,
            )

        # 分句逐段合成再拼接（类似 GPT-SoVITS 服务端 cut5 效果）
        chunks = split_sentences_for_tts(text, max_chars=self.sentence_max_chars)
        if not chunks:
            return b""
        if len(chunks) == 1:
            return await asyncio.to_thread(
                _synthesize_sync,
                chunks[0],
                self.mode,
                self.model_id,
                self.device,
                self.dtype,
                self.attn,
                self.language,
                self.speaker,
                final_instruct,
                self.voice_description,
                self.ref_audio_path,
                self.ref_text,
                self.temperature,
                self.top_p,
                self.top_k,
                self.repetition_penalty,
                self.use_torch_compile,
            )

        try:
            import soundfile as sf
        except ImportError:
            logger.warning("[Qwen3-TTS] 分句拼接需要 soundfile")
            return await asyncio.to_thread(
                _synthesize_sync,
                text,
                self.mode,
                self.model_id,
                self.device,
                self.dtype,
                self.attn,
                self.language,
                self.speaker,
                final_instruct,
                self.voice_description,
                self.ref_audio_path,
                self.ref_text,
                self.temperature,
                self.top_p,
                self.top_k,
                self.repetition_penalty,
                self.use_torch_compile,
            )

        def _run_chunk(seg: str) -> bytes:
            return _synthesize_sync(
                seg,
                self.mode,
                self.model_id,
                self.device,
                self.dtype,
                self.attn,
                self.language,
                self.speaker,
                final_instruct,
                self.voice_description,
                self.ref_audio_path,
                self.ref_text,
                self.temperature,
                self.top_p,
                self.top_k,
                self.repetition_penalty,
                self.use_torch_compile,
            )

        # 串行合成各段（同一 GPU 上并行不会更快，逐段可降低单次显存与首包延迟）
        all_wav_bytes: List[bytes] = []
        for seg in chunks:
            seg_bytes = await asyncio.to_thread(_run_chunk, seg)
            if seg_bytes:
                all_wav_bytes.append(seg_bytes)

        if not all_wav_bytes:
            return b""
        if len(all_wav_bytes) == 1:
            return all_wav_bytes[0]

        # 解码各段 WAV，拼接采样后重新编码
        import numpy as np
        samples_list: List[Any] = []
        sr_out = None
        for b in all_wav_bytes:
            data, sr = sf.read(io.BytesIO(b), dtype="float32")
            if sr_out is None:
                sr_out = sr
            elif sr != sr_out:
                logger.warning("[Qwen3-TTS] 段间采样率不一致 %s vs %s，跳过拼接", sr_out, sr)
                return all_wav_bytes[0]
            samples_list.append(data)
        concatenated = np.concatenate(samples_list)
        buf = io.BytesIO()
        sf.write(buf, concatenated, sr_out, format="WAV")
        return buf.getvalue()
