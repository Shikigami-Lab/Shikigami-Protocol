"""新手入门：环境自检、模型包下载（HF snapshot_download）。"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.llm.registry import get_provider
from src.utils.paths import get_project_root

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])

_BUNDLES_PATH = os.path.join(get_project_root(), "config", "model_bundles.yaml")
_download_lock = threading.Lock()
_download_state: Dict[str, Any] = {
    "phase": "idle",
    "bundle_id": None,
    "progress_pct": 0,
    "message": "",
    "error": None,
}

_pip_install_lock = threading.Lock()
_pip_install_state: Dict[str, Any] = {
    "phase": "idle",   # idle | running | success | error
    "target": None,    # e.g. "modelscope" | "ai_memory" | "kokoro" | "qwen_tts" | "sherpa_onnx"
    "message": "",
    "error": None,
}


def _models_root() -> str:
    env = (os.environ.get("SHIKIGAMI_MODELS_ROOT") or "").strip()
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    root = os.path.join(os.getcwd(), "models")
    return os.path.abspath(root)


def _load_bundle_list() -> List[Dict[str, Any]]:
    if not os.path.isfile(_BUNDLES_PATH):
        return []
    try:
        with open(_BUNDLES_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return list(data.get("bundles") or [])
    except Exception as e:
        logger.warning("[setup] model_bundles load failed: %s", e)
        return []


def _bundle_dir(bundle: Dict[str, Any]) -> str:
    sub = (bundle.get("local_subdir") or bundle.get("id") or "model").strip().replace("..", "_")
    return os.path.join(_models_root(), sub)


def _dir_looks_downloaded(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    try:
        names = os.listdir(path)
    except OSError:
        return False
    if not names:
        return False
    markers = (".bin", ".safetensors", ".onnx", ".pt", ".json", ".mdl")
    for n in names:
        low = n.lower()
        if any(low.endswith(m) for m in markers):
            return True
        if low == "snapshots" and os.path.isdir(os.path.join(path, "snapshots")):
            return True
    return len(names) >= 3


def _user_pkg_has(pkg_name: str) -> bool:
    """Frozen 模式下：检查 user_packages/ 里是否存在该包的目录或 dist-info。
    用于 import 失败时的文件系统 fallback，避免 ABI 不兼容导致误报"未安装"。"""
    if not getattr(sys, "frozen", False):
        return False
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    user_pkg = os.path.join(exe_dir, "user_packages")
    if not os.path.isdir(user_pkg):
        return False
    safe = pkg_name.replace("-", "_").lower()
    try:
        entries = os.listdir(user_pkg)
    except OSError:
        return False
    for e in entries:
        el = e.lower()
        # 包目录（如 modelscope/）或 dist-info（如 modelscope-1.x.dist-info）
        if el == safe or el.startswith(safe + "-") or el.startswith(safe + "_"):
            return True
    return False


def _chromadb_installed() -> bool:
    try:
        import chromadb  # noqa: F401
        return True
    except ImportError:
        return _user_pkg_has("chromadb")


def _modelscope_installed() -> bool:
    try:
        import modelscope  # noqa: F401
        return True
    except ImportError:
        return _user_pkg_has("modelscope")


def _qwen_tts_installed() -> bool:
    try:
        import qwen_tts  # noqa: F401
        return True
    except ImportError:
        return _user_pkg_has("qwen_tts")


def _torch_installed() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return _user_pkg_has("torch")


def _torch_cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def _detect_cuda_info() -> Dict[str, Any]:
    """检测 NVIDIA GPU 及驱动支持的最高 CUDA 版本，返回推荐的 PyTorch wheel index-url。

    Returns:
        {
            "gpu_name": str,          # e.g. "NVIDIA GeForce RTX 5080" or ""
            "driver_cuda": str,       # driver max CUDA version, e.g. "12.8" or ""
            "recommended_cu": str,    # e.g. "cu128", "cu124", "cu118", or ""
            "recommended_url": str,   # PyTorch whl index URL or ""
        }
    """
    result: Dict[str, Any] = {"gpu_name": "", "driver_cuda": "", "recommended_cu": "", "recommended_url": ""}
    try:
        import subprocess as _sp
        out = _sp.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return result
        result["gpu_name"] = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""

        # nvidia-smi also prints CUDA version in the header when run without args
        out2 = _sp.run(["nvidia-smi"], capture_output=True, text=True, timeout=5)
        # Header line: "CUDA Version: 12.8"
        for line in out2.stdout.splitlines():
            if "CUDA Version:" in line:
                parts = line.split("CUDA Version:")
                if len(parts) > 1:
                    result["driver_cuda"] = parts[1].strip().split()[0]
                break
    except Exception:
        return result

    driver_cuda = result["driver_cuda"]
    if not driver_cuda:
        return result

    try:
        major, minor = (int(x) for x in driver_cuda.split(".")[:2])
        version_int = major * 10 + minor  # e.g. 12.8 → 128
    except Exception:
        return result

    if version_int >= 128:
        cu, url = "cu128", "https://download.pytorch.org/whl/cu128"
    elif version_int >= 124:
        cu, url = "cu124", "https://download.pytorch.org/whl/cu124"
    elif version_int >= 118:
        cu, url = "cu118", "https://download.pytorch.org/whl/cu118"
    else:
        cu, url = "cu118", "https://download.pytorch.org/whl/cu118"

    result["recommended_cu"] = cu
    result["recommended_url"] = url
    return result


def _ai_memory_installed() -> bool:
    """全套 AI 记忆依赖是否已安装（chromadb + transformers + sentence_transformers）。"""
    pkgs = ("chromadb", "transformers", "sentence_transformers")
    for mod in pkgs:
        try:
            __import__(mod)
        except ImportError:
            if not _user_pkg_has(mod):
                return False
    return True


def _llm_preset_looks_configured(preset: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    if not preset:
        return False, "no_preset"
    url = (preset.get("base_url") or "").lower()
    key = (preset.get("api_key") or "").strip()
    if "localhost" in url or "127.0.0.1" in url:
        return True, "ollama_or_local"
    if key:
        return True, "has_api_key"
    return False, "need_key_or_local"


def _sherpa_onnx_installed() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
        return True
    except ImportError:
        return False


_SHERPA_SENSE_VOICE_SEARCH_DIRS = [
    "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue",
]


def _sherpa_sense_voice_ready(cfg_model_path: str = "") -> bool:
    if cfg_model_path and os.path.isdir(cfg_model_path):
        model_f = os.path.join(cfg_model_path, "model.int8.onnx")
        tokens_f = os.path.join(cfg_model_path, "tokens.txt")
        return os.path.isfile(tokens_f) and (os.path.isfile(model_f) or
               os.path.isfile(os.path.join(cfg_model_path, "model.onnx")))
    cwd = os.getcwd()
    for rel in _SHERPA_SENSE_VOICE_SEARCH_DIRS:
        fp = os.path.join(cwd, rel)
        if os.path.isfile(os.path.join(fp, "tokens.txt")) and (
            os.path.isfile(os.path.join(fp, "model.int8.onnx")) or
            os.path.isfile(os.path.join(fp, "model.onnx"))
        ):
            return True
    return False


def _stt_ready(stt_cfg: Dict[str, Any]) -> Tuple[bool, str]:
    if not stt_cfg.get("enabled"):
        return True, "stt_disabled"
    mp = (stt_cfg.get("model_path") or "").strip()
    if not _sherpa_onnx_installed():
        return False, "need_sherpa_onnx"
    if _sherpa_sense_voice_ready(mp):
        return True, "sherpa_onnx_ready"
    return False, "sherpa_onnx_model_missing"


def _tts_status(config) -> Dict[str, Any]:
    """检测各 TTS 方案的就绪状态。"""
    tts_type = (getattr(config, "default_tts", None) or "edge_tts").strip()
    tts_cfg_map = getattr(config, "tts_config", {}) or {}

    gpt_cfg_early = tts_cfg_map.get("gpt_sovits") or {}
    result: Dict[str, Any] = {
        "active": tts_type,
        "edge_tts_ready": True,   # 总是就绪（edge-tts 随 pip 安装）
        "gpt_sovits_port_open": False,
        "gpt_sovits_port": 9880,
        "gpt_sovits_dir": gpt_cfg_early.get("dir", ""),
        "gpt_sovits_script_bat": os.path.isfile("scripts/start_gptsovits.bat"),
        "gpt_sovits_script_sh": os.path.isfile("scripts/start_gptsovits.sh"),
        "kokoro_installed": False,
        "kokoro_misaki_zh": False,
        "kokoro_misaki_ja": False,
        "qwen3_model_ready": False,
        "qwen3_model_path": "",
    }

    # GPT-SoVITS：检查端口
    port = int(gpt_cfg_early.get("port", 9880))
    result["gpt_sovits_port"] = port
    try:
        import socket
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            result["gpt_sovits_port_open"] = True
    except OSError:
        pass

    # KokoroTTS：检查 pip 包 + 模型文件
    try:
        import kokoro_onnx  # noqa: F401
        result["kokoro_installed"] = True
    except ImportError:
        pass
    try:
        from misaki import zh  # noqa: F401
        result["kokoro_misaki_zh"] = True
    except ImportError:
        pass
    try:
        from misaki import ja  # noqa: F401
        result["kokoro_misaki_ja"] = True
    except ImportError:
        pass
    # Check if Kokoro model files are present
    _kokoro_model_found = False
    for _search_dir in [os.path.join(os.getcwd(), "models"), os.getcwd()]:
        for _mf in ["kokoro-v1.0.onnx", "kokoro-v0_19.onnx"]:
            for _vf in ["voices-v1.0.bin", "voices-v0_19.bin"]:
                if os.path.isfile(os.path.join(_search_dir, _mf)) and \
                   os.path.isfile(os.path.join(_search_dir, _vf)):
                    _kokoro_model_found = True
                    break
    result["kokoro_models_ready"] = _kokoro_model_found

    # Qwen3-TTS：检查模型目录
    qwen_cfg = tts_cfg_map.get("qwen3_tts") or {}
    model_id = (qwen_cfg.get("model_id") or "").strip()
    qwen_paths = [
        "models/Qwen-Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "models/Qwen-Qwen3-TTS-12Hz-1.7B-Base",
        "models/Qwen-Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    ]
    if model_id and not model_id.startswith("Qwen/") and not os.path.isabs(model_id):
        qwen_paths.insert(0, model_id)
    elif model_id and os.path.isabs(model_id):
        qwen_paths.insert(0, model_id)
    for p in qwen_paths:
        fp = p if os.path.isabs(p) else os.path.join(os.getcwd(), p)
        if _dir_looks_downloaded(fp):
            result["qwen3_model_ready"] = True
            result["qwen3_model_path"] = p
            break

    return result


@router.get("/status")
async def setup_status(request: Request) -> Dict[str, Any]:
    config = request.app.state.config
    mem = config.get_memory_config()
    emb = mem.get("embedding") or {}
    stt_cfg = getattr(config, "stt", None) or {}

    bundles_raw = _load_bundle_list()
    bundles_out = []
    for b in bundles_raw:
        bid = b.get("id", "")
        path = _bundle_dir(b)
        bundles_out.append({
            "id": bid,
            "category": b.get("category", ""),
            "size_hint": b.get("size_hint", ""),
            "title_zh": b.get("title_zh", bid),
            "title_en": b.get("title_en", bid),
            "description_zh": b.get("description_zh", ""),
            "description_en": b.get("description_en", ""),
            "memory_local_model_hint": b.get("memory_local_model_hint", ""),
            "local_path": path,
            "installed": _dir_looks_downloaded(path),
            "recommended": b.get("recommended", False),
            # 下载来源字段，前端用来决定显示哪些按钮
            "repo_id": b.get("repo_id", ""),
            "modelscope_repo_id": b.get("modelscope_repo_id", ""),
            "direct_url": b.get("direct_url", ""),
        })

    default_name = config.default_llm
    preset = config.llm_presets.get(default_name)
    cfg_ok, cfg_reason = _llm_preset_looks_configured(preset)

    local_model = (emb.get("local_model") or "all-MiniLM-L6-v2").strip()
    lp = local_model if os.path.isabs(local_model) else os.path.join(os.getcwd(), local_model.replace("/", os.sep))
    local_on_disk = bool(os.path.isfile(lp)) or (_dir_looks_downloaded(lp) if os.path.isdir(lp) else False)

    # 所有已知嵌入模型目录逐一探测（不只是 config 里指定的那一个）
    _known_embed_dirs = [
        ("bge_small_zh",   "models/bge-small-zh-v1.5"),
        ("minilm_en",      "models/all-MiniLM-L6-v2"),
    ]
    embed_installed: dict = {}
    for eid, rel in _known_embed_dirs:
        fp = os.path.join(os.getcwd(), rel)
        embed_installed[eid] = _dir_looks_downloaded(fp)

    # STT：检查引擎就绪状态
    _stt_cfg_dict = stt_cfg if isinstance(stt_cfg, dict) else {}
    stt_ok, stt_reason = _stt_ready(_stt_cfg_dict)
    sv_cfg_path = (_stt_cfg_dict.get("model_path") or "").strip()
    sherpa_installed_dirs: dict = {}
    for rel in _SHERPA_SENSE_VOICE_SEARCH_DIRS:
        sherpa_installed_dirs[rel] = _dir_looks_downloaded(os.path.join(os.getcwd(), rel))

    with _download_lock:
        dl = dict(_download_state)

    with _pip_install_lock:
        pip_st = dict(_pip_install_state)

    profile_dir = os.path.join(get_project_root(), "profiles")
    profile_count = 0
    if os.path.isdir(profile_dir):
        profile_count = len([x for x in os.listdir(profile_dir) if x.endswith(".json")])

    tts_st = _tts_status(config)

    return {
        "models_root": _models_root(),
        "hf_endpoint": os.environ.get("HF_ENDPOINT", ""),
        "hf_hub_offline": os.environ.get("HF_HUB_OFFLINE", "") == "1",
        "bundles": bundles_out,
        "download": dl,
        "chromadb_installed": _chromadb_installed(),
        "ai_memory_installed": _ai_memory_installed(),
        "modelscope_installed": _modelscope_installed(),
        "qwen_tts_installed": _qwen_tts_installed(),
        "torch_installed": _torch_installed(),
        "torch_cuda_available": _torch_cuda_available(),
        "cuda_info": _detect_cuda_info(),
        "profile_count": profile_count,
        "default_llm": default_name,
        "llm_preset_configured": cfg_ok,
        "llm_config_hint": cfg_reason,
        "memory_enabled": bool(mem.get("enabled", True)),
        "memory_vector_enabled": bool(mem.get("vector_enabled", True)),
        "embedding_provider": emb.get("provider", "gemini"),
        "embedding_local_model": local_model,
        "embedding_local_on_disk": local_on_disk,
        "stt_enabled": bool(_stt_cfg_dict.get("enabled")),
        "stt_ready": stt_ok,
        "stt_hint": stt_reason,
        "stt_model_path": (_stt_cfg_dict.get("model_path") or ""),
        "sherpa_onnx_installed": _sherpa_onnx_installed(),
        "sherpa_sense_voice_ready": _sherpa_sense_voice_ready(sv_cfg_path),
        "sherpa_installed_dirs": sherpa_installed_dirs,
        "embed_installed": embed_installed,
        "tts": tts_st,
        "pip_install": pip_st,
    }


@router.post("/verify")
async def setup_verify(request: Request) -> Dict[str, Any]:
    config = request.app.state.config
    mem = config.get_memory_config()
    emb = mem.get("embedding") or {}

    out: Dict[str, Any] = {
        "llm_ok": False,
        "llm_error": None,
        "llm_latency_ms": None,
        "embedding_ok": False,
        "embedding_skipped": False,
        "embedding_error": None,
        "embedding_latency_ms": None,
    }

    name = config.default_llm
    preset = config.llm_presets.get(name)
    if preset:
        try:
            provider = get_provider(preset)
            t0 = time.perf_counter()
            resp = await asyncio.wait_for(
                provider.chat([{"role": "user", "content": "Hi"}]),
                timeout=25.0,
            )
            latency = (time.perf_counter() - t0) * 1000
            rs = (resp or "").strip()
            if rs and not rs.startswith("[LLM Error"):
                out["llm_ok"] = True
                out["llm_latency_ms"] = round(latency, 1)
            else:
                out["llm_error"] = (resp or "empty")[:500]
        except asyncio.TimeoutError:
            out["llm_error"] = "LLM 请求超时（25s）"
        except Exception as e:
            out["llm_error"] = str(e)[:500]
    else:
        out["llm_error"] = f"未找到默认预设: {name}"

    if not mem.get("vector_enabled", True):
        out["embedding_skipped"] = True
        out["embedding_ok"] = True
    else:
        from src.api.settings_ext import _run_embedding_test

        test_cfg = {
            "provider": emb.get("provider", "gemini"),
            "gemini_model": emb.get("gemini_model", "gemini-embedding-001"),
            "local_model": emb.get("local_model", "all-MiniLM-L6-v2"),
            "offline": emb.get("offline") is True,
            "device": emb.get("device", "auto"),
        }
        try:
            ok, lat, err = await asyncio.wait_for(
                asyncio.to_thread(_run_embedding_test, test_cfg),
                timeout=45.0,
            )
            if ok:
                out["embedding_ok"] = True
                out["embedding_latency_ms"] = round(lat, 1)
            else:
                out["embedding_error"] = err or "失败"
        except asyncio.TimeoutError:
            out["embedding_error"] = "Embedding 检测超时（45s），可尝试本页下载本地模型后改为本地嵌入"
        except Exception as e:
            out["embedding_error"] = str(e)[:500]

    return out


def _github_mirror_urls(url: str) -> list:
    """为 GitHub releases URL 生成镜像候选列表（原始 + 国内代理）。"""
    mirrors = [url]
    if "github.com" in url:
        for proxy in ["https://ghproxy.net/", "https://mirror.ghproxy.com/"]:
            mirrors.append(proxy + url)
    return mirrors


def _download_worker_direct(bundle_id: str, url: str, local_dir: str) -> None:
    """直链下载（支持 .tar.bz2 / .tar.gz / .zip）并解压到 local_dir。
    若主 URL 失败，自动尝试 GitHub 镜像代理。"""
    global _download_state
    import urllib.request
    import tarfile
    import zipfile
    import tempfile

    try:
        os.makedirs(local_dir, exist_ok=True)
        suffix = ".tar.bz2" if url.endswith(".tar.bz2") else \
                 ".tar.gz"  if url.endswith(".tar.gz")  else \
                 ".zip"     if url.endswith(".zip")      else ".bin"

        candidates = _github_mirror_urls(url)
        actual_url = None
        last_err = None
        for candidate in candidates:
            with _download_lock:
                _download_state["message"] = f"正在连接… {candidate[:40]}…"
                _download_state["progress_pct"] = 2
            try:
                req_probe = urllib.request.Request(
                    candidate, headers={"User-Agent": "shikigami/1.0"}, method="HEAD"
                )
                with urllib.request.urlopen(req_probe, timeout=15):
                    pass
                actual_url = candidate
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    last_err = e
                    logger.warning("[setup] URL probe 404 %s", candidate)
                    continue  # definitely not there, try next
                # Other HTTP errors (405 Method Not Allowed, 403, etc.): still try this URL for actual GET
                actual_url = candidate
                break
            except Exception as e:
                last_err = e
                logger.warning("[setup] URL probe failed %s: %s", candidate, e)

        if actual_url is None:
            raise RuntimeError(f"所有下载源均不可用: {last_err}")

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.close()
        tmp_path = tmp.name

        # 流式下载，每 512KB 更新一次进度
        req = urllib.request.Request(actual_url, headers={"User-Agent": "shikigami/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk = 512 * 1024
            with open(tmp_path, "wb") as f:
                while True:
                    data = resp.read(chunk)
                    if not data:
                        break
                    f.write(data)
                    downloaded += len(data)
                    if total > 0:
                        pct = min(90, int(downloaded / total * 90))
                        with _download_lock:
                            _download_state["progress_pct"] = pct
                            _download_state["message"] = f"下载中 {downloaded // 1024 // 1024}MB / {total // 1024 // 1024}MB"

        with _download_lock:
            _download_state["message"] = "解压中…"
            _download_state["progress_pct"] = 92

        parent = os.path.dirname(local_dir)
        if suffix in (".tar.bz2", ".tar.gz"):
            with tarfile.open(tmp_path, "r:*") as tf:
                # 去掉顶层目录，直接解压到 local_dir
                members = tf.getmembers()
                top = members[0].name.split("/")[0] if members else ""
                for m in members:
                    parts = m.name.split("/", 1)
                    rel = parts[1] if len(parts) > 1 and parts[0] == top else m.name
                    if not rel:
                        continue
                    m.name = rel
                    tf.extract(m, local_dir)
        elif suffix == ".zip":
            with zipfile.ZipFile(tmp_path, "r") as zf:
                zf.extractall(local_dir)

        os.unlink(tmp_path)
        with _download_lock:
            _download_state["phase"] = "success"
            _download_state["progress_pct"] = 100
            _download_state["message"] = "完成"
            _download_state["error"] = None
    except Exception as e:
        logger.exception("[setup] direct download failed bundle=%s", bundle_id)
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        with _download_lock:
            _download_state["phase"] = "error"
            _download_state["error"] = str(e)
            _download_state["message"] = "失败"


def _download_worker(bundle_id: str, repo_id: str, local_dir: str) -> None:
    global _download_state
    try:
        os.makedirs(local_dir, exist_ok=True)
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

        from huggingface_hub import snapshot_download

        with _download_lock:
            _download_state["message"] = "正在连接镜像…"
            _download_state["progress_pct"] = 5

        dl_kwargs: Dict[str, Any] = {
            "repo_id": repo_id,
            "local_dir": local_dir,
            "local_dir_use_symlinks": False,
        }
        try:
            from tqdm import tqdm

            class ReportingTqdm(tqdm):
                def update(self, n: int = 1) -> bool:
                    r = super().update(n)
                    try:
                        tot = int(self.total) if self.total else 0
                        if tot > 0:
                            with _download_lock:
                                _download_state["progress_pct"] = min(99, int(100 * self.n / tot))
                                desc = (getattr(self, "desc", None) or "") or ""
                                _download_state["message"] = (desc + f" {self.n}/{tot}").strip()
                    except Exception:
                        pass
                    return r

            dl_kwargs["tqdm_class"] = ReportingTqdm
        except ImportError:
            pass

        snapshot_download(**dl_kwargs)
        with _download_lock:
            _download_state["phase"] = "success"
            _download_state["progress_pct"] = 100
            _download_state["message"] = "完成"
            _download_state["error"] = None
    except Exception as e:
        logger.exception("[setup] download failed bundle=%s", bundle_id)
        with _download_lock:
            _download_state["phase"] = "error"
            _download_state["error"] = str(e)
            _download_state["message"] = "失败"


def _download_worker_modelscope(bundle_id: str, ms_repo_id: str, local_dir: str) -> None:
    """用 ModelScope SDK 下载模型到 local_dir。"""
    global _download_state
    try:
        os.makedirs(local_dir, exist_ok=True)
        from modelscope.hub.snapshot_download import snapshot_download as ms_snapshot_download

        with _download_lock:
            _download_state["message"] = "正在连接 ModelScope…"
            _download_state["progress_pct"] = 5

        # modelscope snapshot_download: model_id + local_dir (no cache_dir param)
        ms_snapshot_download(ms_repo_id, local_dir=local_dir)

        with _download_lock:
            _download_state["phase"] = "success"
            _download_state["progress_pct"] = 100
            _download_state["message"] = "完成"
            _download_state["error"] = None
    except Exception as e:
        logger.exception("[setup] ModelScope download failed bundle=%s", bundle_id)
        with _download_lock:
            _download_state["phase"] = "error"
            _download_state["error"] = str(e)
            _download_state["message"] = "失败"


class DownloadBody(BaseModel):
    bundle: str
    source: str = "auto"   # auto | hf | modelscope | direct


@router.post("/download")
async def start_download(body: DownloadBody) -> Dict[str, Any]:
    bundles = _load_bundle_list()
    b = next((x for x in bundles if x.get("id") == body.bundle), None)
    if not b:
        return JSONResponse(status_code=404, content={"ok": False, "error": "unknown bundle"})

    local_dir = _bundle_dir(b)
    direct_url = (b.get("direct_url") or "").strip()
    repo_id = (b.get("repo_id") or "").strip()
    ms_repo_id = (b.get("modelscope_repo_id") or "").strip()

    source = body.source or "auto"

    # auto 优先级：direct_url (有镜像回退) > HF > ModelScope
    if not direct_url and not repo_id and not ms_repo_id:
        return JSONResponse(status_code=400, content={"ok": False, "error": "no repo_id or direct_url"})

    source_used = source
    thread = None
    # 先决定要用哪种下载器（尽量避免启动下载后才因为缺依赖直接失败）
    if source == "direct":
        if not direct_url:
            return JSONResponse(status_code=400, content={"ok": False, "error": "该模型包无 direct_url"})
        thread = threading.Thread(
            target=_download_worker_direct,
            args=(body.bundle, direct_url, local_dir),
            daemon=True,
        )
        source_used = "direct"
    elif source == "hf":
        if not repo_id:
            return JSONResponse(status_code=400, content={"ok": False, "error": "该模型包无 repo_id (HF)"})
        thread = threading.Thread(
            target=_download_worker,
            args=(body.bundle, repo_id, local_dir),
            daemon=True,
        )
        source_used = "hf"
    elif source == "modelscope":
        if not ms_repo_id:
            return JSONResponse(status_code=400, content={"ok": False, "error": "该模型包无 modelscope_repo_id"})
        try:
            import modelscope  # noqa: F401
        except ModuleNotFoundError:
            # 用户显式选择了 ModelScope：缺依赖时不降级，直接提示安装（符合“点哪个就用哪个”）
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "error": "未安装 modelscope 依赖",
                    "code": "need_modelscope",
                    "hint": "请先在「设置 → 入门」安装 ModelScope 组件（或执行：pip install modelscope）。",
                },
            )
        else:
            thread = threading.Thread(
                target=_download_worker_modelscope,
                args=(body.bundle, ms_repo_id, local_dir),
                daemon=True,
            )
            source_used = "modelscope"
    else:
        # auto
        if direct_url:
            thread = threading.Thread(
                target=_download_worker_direct,
                args=(body.bundle, direct_url, local_dir),
                daemon=True,
            )
            source_used = "direct"
        elif repo_id:
            thread = threading.Thread(
                target=_download_worker,
                args=(body.bundle, repo_id, local_dir),
                daemon=True,
            )
            source_used = "hf"
        elif ms_repo_id:
            try:
                import modelscope  # noqa: F401
            except ModuleNotFoundError:
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": "未安装 modelscope 依赖，且该模型包无 HF repo_id 可回退",
                        "hint": "请执行：pip install modelscope",
                    },
                )
            thread = threading.Thread(
                target=_download_worker_modelscope,
                args=(body.bundle, ms_repo_id, local_dir),
                daemon=True,
            )
            source_used = "modelscope"

    assert thread is not None

    with _download_lock:
        if _download_state["phase"] == "running":
            return JSONResponse(
                status_code=409,
                content={"ok": False, "error": "已有任务进行中", "state": dict(_download_state)},
            )
        _download_state["phase"] = "running"
        _download_state["bundle_id"] = body.bundle
        _download_state["progress_pct"] = 0
        _download_state["message"] = "启动…"
        _download_state["error"] = None

    thread.start()
    return {"ok": True, "bundle": body.bundle, "local_dir": local_dir, "source_used": source_used}


@router.get("/download/status")
async def download_status() -> Dict[str, Any]:
    with _download_lock:
        return dict(_download_state)


# ─── Pip install / uninstall ─────────────────────────────────────────────────

class PipInstallBody(BaseModel):
    packages: List[str]
    target: str = ""   # 前端传入，用于区分哪个按钮触发了安装
    index_url: str = ""  # 可选，如 https://download.pytorch.org/whl/cu124


class PipUninstallBody(BaseModel):
    packages: List[str]   # pip 包名列表
    dirs: List[str] = []  # 额外要删除的目录（相对于项目根，如 models/all-MiniLM-L6-v2）


def _pip_install_worker(packages: List[str], index_url: str = "") -> None:
    global _pip_install_state
    try:
        with _pip_install_lock:
            _pip_install_state["message"] = "安装中…"

        if getattr(sys, "frozen", False):
            # PyInstaller frozen 模式：sys.executable 是 server.exe，不能直接用来跑 pip。
            # Windows 安装包内附带了 python_embed/python.exe（独立 Python 3.10），用它来
            # 安装包到 user_packages/，与用户系统 Python 完全隔离。
            # Mac/Linux 回退到 PATH 里的 python3。
            exe_dir = os.path.dirname(sys.executable)
            embed_python = os.path.join(exe_dir, "python_embed", "python.exe")
            if not os.path.isfile(embed_python):
                # Mac / Linux fallback
                import shutil
                embed_python = shutil.which("python3") or shutil.which("python") or ""
            if not embed_python:
                with _pip_install_lock:
                    _pip_install_state["phase"] = "error"
                    _pip_install_state["message"] = "安装失败"
                    _pip_install_state["error"] = "找不到 Python 解释器"
                return
            target_dir = os.path.join(exe_dir, "user_packages")
            os.makedirs(target_dir, exist_ok=True)
            cmd = [embed_python, "-m", "pip", "install", "--target", target_dir] + packages
            if index_url:
                cmd += ["--index-url", index_url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode == 0:
                # 安装成功后立即把 user_packages/ 注入当前进程的 sys.path，
                # 使 _xxx_installed() 检查能在同一 session 里立即返回 True，
                # 前端才能显示"安装完成"提示。
                import importlib
                if target_dir not in sys.path:
                    sys.path.insert(0, target_dir)
                importlib.invalidate_caches()
            with _pip_install_lock:
                if result.returncode == 0:
                    _pip_install_state["phase"] = "success"
                    _pip_install_state["message"] = "安装完成，重启应用后生效"
                    _pip_install_state["error"] = None
                else:
                    _pip_install_state["phase"] = "error"
                    _pip_install_state["message"] = "安装失败"
                    _pip_install_state["error"] = (result.stderr or result.stdout or "")[-800:]
            return

        cmd = [sys.executable, "-m", "pip", "install"] + packages
        if index_url:
            cmd += ["--index-url", index_url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            import importlib
            importlib.invalidate_caches()
        with _pip_install_lock:
            if result.returncode == 0:
                _pip_install_state["phase"] = "success"
                _pip_install_state["message"] = "安装完成"
                _pip_install_state["error"] = None
            else:
                _pip_install_state["phase"] = "error"
                _pip_install_state["message"] = "安装失败"
                _pip_install_state["error"] = (result.stderr or result.stdout or "")[-800:]
    except Exception as e:
        with _pip_install_lock:
            _pip_install_state["phase"] = "error"
            _pip_install_state["message"] = "安装失败"
            _pip_install_state["error"] = str(e)[:500]


@router.post("/pip-install")
async def pip_install(body: PipInstallBody) -> Dict[str, Any]:
    with _pip_install_lock:
        if _pip_install_state["phase"] == "running":
            return JSONResponse(status_code=409, content={"ok": False, "error": "已有安装任务进行中"})
        _pip_install_state["phase"] = "running"
        _pip_install_state["target"] = body.target or ""
        _pip_install_state["message"] = "启动…"
        _pip_install_state["error"] = None
    thread = threading.Thread(target=_pip_install_worker, args=(list(body.packages), body.index_url or ""), daemon=True)
    thread.start()
    return {"ok": True}


@router.get("/pip-install/status")
async def pip_install_status() -> Dict[str, Any]:
    with _pip_install_lock:
        return dict(_pip_install_state)


@router.post("/pip-uninstall")
async def pip_uninstall(body: PipUninstallBody) -> Dict[str, Any]:
    """卸载 pip 包并可选删除模型目录。同步执行（卸载通常很快）。"""
    import shutil as _shutil
    from src.utils.paths import get_project_root

    errors: list = []

    # 1. pip uninstall
    if body.packages:
        if getattr(sys, "frozen", False):
            exe_dir = os.path.dirname(sys.executable)
            embed_python = os.path.join(exe_dir, "python_embed", "python.exe")
            if not os.path.isfile(embed_python):
                embed_python = _shutil.which("python3") or _shutil.which("python") or ""
            if embed_python:
                target_dir = os.path.join(exe_dir, "user_packages")
                cmd = [embed_python, "-m", "pip", "uninstall", "-y",
                       "--target", target_dir] + list(body.packages)
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                # pip uninstall --target 不被支持时降级（直接删包目录里的文件夹）
                if result.returncode != 0 and os.path.isdir(target_dir):
                    for pkg in body.packages:
                        pkg_base = pkg.split("[")[0].replace("-", "_").lower()
                        for entry in os.listdir(target_dir):
                            if entry.lower().replace("-", "_").startswith(pkg_base):
                                full = os.path.join(target_dir, entry)
                                try:
                                    if os.path.isdir(full):
                                        _shutil.rmtree(full)
                                    else:
                                        os.remove(full)
                                except Exception as e:
                                    errors.append(str(e))
        else:
            cmd = [sys.executable, "-m", "pip", "uninstall", "-y"] + list(body.packages)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                errors.append((result.stderr or result.stdout or "")[-400:])

    # 2. 删除模型目录
    root = get_project_root()
    for rel_dir in (body.dirs or []):
        abs_dir = os.path.normpath(os.path.join(root, rel_dir))
        # 安全校验：只允许删 models/ 子目录
        if not abs_dir.startswith(os.path.join(root, "models")):
            errors.append(f"拒绝删除非 models/ 目录: {rel_dir}")
            continue
        if os.path.isdir(abs_dir):
            try:
                _shutil.rmtree(abs_dir)
            except Exception as e:
                errors.append(f"删除 {rel_dir} 失败: {e}")

    import importlib
    importlib.invalidate_caches()

    if errors:
        return {"ok": False, "errors": errors}
    return {"ok": True}


# ─── Launch GPT-SoVITS ───────────────────────────────────────────────────────

@router.post("/launch-gptsovits")
async def launch_gptsovits(request: Request) -> Dict[str, Any]:
    import subprocess
    import platform
    is_win = platform.system() == "Windows"
    script = "scripts/start_gptsovits.bat" if is_win else "scripts/start_gptsovits.sh"
    if not os.path.isfile(script):
        return JSONResponse(status_code=404, content={"ok": False, "error": f"Script not found: {script}"})
    script_abs = os.path.abspath(script)

    # Pass GPTSOVITS_DIR env var if configured
    env = os.environ.copy()
    try:
        cfg = request.app.state.config
        gpt_dir = (cfg.tts_config.get("gpt_sovits") or {}).get("dir", "").strip()
        if gpt_dir:
            env["GPTSOVITS_DIR"] = os.path.abspath(gpt_dir)
    except Exception:
        pass

    try:
        if is_win:
            subprocess.Popen(["cmd", "/c", "start", "", script_abs], shell=False,
                             cwd=os.getcwd(), env=env)
        else:
            subprocess.Popen(["bash", script_abs], cwd=os.getcwd(), env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ─── Apply STT model path ─────────────────────────────────────────────────────

class ApplySttModelBody(BaseModel):
    model_path: str


@router.post("/apply-stt-model")
async def apply_stt_model(request: Request, body: ApplySttModelBody) -> Dict[str, Any]:
    """Write stt.model_path to app.yaml and hot-reload config."""
    from src.api.settings_ext import _load_yaml, _save_yaml
    config = request.app.state.config
    path = body.model_path.strip()
    y, data = _load_yaml()
    if "stt" not in data:
        data["stt"] = {}
    data["stt"]["model_path"] = path
    _save_yaml(y, data)
    stt_cfg = config.get_stt_config()
    if isinstance(stt_cfg, dict):
        stt_cfg["model_path"] = path
    return {"ok": True, "model_path": path}


