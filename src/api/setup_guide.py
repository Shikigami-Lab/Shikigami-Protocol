"""新手入门：环境自检、模型包下载（HF snapshot_download）。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.llm.registry import get_provider
from src.utils.paths import get_project_root

logger = logging.getLogger(__name__)

# Self-check may run on every /api/setup poll; log import failures at WARNING once per key.
_selfcheck_import_warned: set[str] = set()

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
    root = os.path.abspath(_models_root())
    if sub in (".", ""):
        return root
    return os.path.join(root, sub)


def _bundle_installed_on_disk(bundle: Dict[str, Any]) -> bool:
    """Per-bundle ready state: optional single-file check, else directory heuristic."""
    icf = (bundle.get("install_check_file") or "").strip().replace("\\", "/")
    if icf:
        # reject path traversal
        if icf.startswith("/") or ".." in icf.split("/"):
            return False
        fp = os.path.join(_models_root(), *icf.split("/"))
        try:
            return os.path.isfile(fp) and os.path.getsize(fp) > 0
        except OSError:
            return False
    return _dir_looks_downloaded(_bundle_dir(bundle))


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


def _torch_scan_roots() -> List[str]:
    """Where *this* interpreter installs wheels: purelib/platlib, optional user site, frozen ``user_packages``.

    Avoids ``site.getsitepackages()`` pulling in extra stacked env paths (e.g. conda) that confuse
    \"on disk\" checks vs the venv you actually ``pip install`` into from a terminal.
    """
    import site
    import sysconfig

    roots: List[str] = []
    for key in ("purelib", "platlib"):
        try:
            p = sysconfig.get_path(key)
            if p:
                a = os.path.abspath(p)
                if os.path.isdir(a):
                    roots.append(a)
        except Exception:
            pass
    try:
        if site.ENABLE_USER_SITE:
            u = site.getusersitepackages()
            if u:
                a = os.path.abspath(u)
                if os.path.isdir(a):
                    roots.append(a)
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        up = os.path.join(exe_dir, "user_packages")
        if os.path.isdir(up):
            roots.append(os.path.abspath(up))
    out: List[str] = []
    seen = set()
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _purge_stale_torch_modules_if_no_artifacts() -> None:
    """If torch files are gone from scan roots but this process still has ``torch*`` in ``sys.modules``,
    drop the cache so /status matches an external ``pip uninstall`` without requiring restart.

    Safe: only purges when `_pip_torch_artifacts_on_disk` is false (no ``torch/`` / ``torch-*.dist-info``
    under this interpreter's standard roots). Editable installs that keep code only on ``sys.path`` without
    that layout may need a process restart to resync.
    """
    if _pip_torch_artifacts_on_disk():
        return
    keys = [k for k in list(sys.modules) if k == "torch" or k.startswith("torch.")]
    if not keys:
        return
    import importlib

    for k in keys:
        sys.modules.pop(k, None)
    importlib.invalidate_caches()


def _pip_install_clear_stale_errors() -> None:
    """Drop a stuck ``pip_install.phase == error`` banner when the env no longer matches the failure."""

    def _idle() -> None:
        _pip_install_state["phase"] = "idle"
        _pip_install_state["message"] = ""
        _pip_install_state["error"] = None
        _pip_install_state["target"] = None

    with _pip_install_lock:
        st = _pip_install_state
        if st.get("phase") != "error":
            return
        tgt = (st.get("target") or "").strip()
        if tgt not in ("torch_cuda", "qwen_tts", "uninstall"):
            return
        if not _torch_installed():
            _idle()
            return
        if _torch_import_ok():
            _idle()
            return
        if tgt == "torch_cuda" and not _torch_import_ok():
            _idle()
            return


def _pip_torch_artifacts_on_disk() -> bool:
    """site-packages / user_packages 是否仍有 torch（import 失败时也视为已安装，便于显示卸载按钮）。

    只匹配顶层 ``torch/`` 或 ``torch-*.dist-info``，避免误报 ``torchgen`` 等。
    """
    if getattr(sys, "frozen", False):
        return _user_pkg_has("torch")
    for root in _torch_scan_roots():
        try:
            for e in os.listdir(root):
                el = e.lower()
                if el == "torch":
                    return True
                if el.startswith("torch-") and el.endswith(".dist-info"):
                    return True
        except OSError:
            continue
    return False


def _torch_import_ok() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def _torch_load_info() -> Dict[str, Any]:
    """Where ``import torch`` resolves in *this* process (helps pip vs runtime mismatches)."""
    import site
    import sysconfig

    pure = ""
    try:
        pure = os.path.abspath(sysconfig.get_path("purelib") or "")
    except Exception:
        pass
    out: Dict[str, Any] = {
        "ok": False,
        "version": "",
        "file": "",
        "from_user_site": False,
        "under_purelib": False,
    }
    try:
        import torch

        out["ok"] = True
        out["version"] = str(getattr(torch, "__version__", "") or "")
        raw = getattr(torch, "__file__", "") or ""
        if raw:
            ap = os.path.abspath(raw)
            out["file"] = ap.replace("\\", "/")
            if pure:
                try:
                    out["under_purelib"] = os.path.normcase(ap).startswith(
                        os.path.normcase(pure + os.sep)
                    )
                except Exception:
                    pass
            if getattr(sys, "frozen", False) and not out["under_purelib"]:
                exe_dir = os.path.dirname(os.path.abspath(sys.executable))
                up = os.path.abspath(os.path.join(exe_dir, "user_packages"))
                try:
                    out["under_purelib"] = os.path.normcase(ap).startswith(os.path.normcase(up + os.sep))
                except Exception:
                    pass
        try:
            if site.ENABLE_USER_SITE and raw:
                us = site.getusersitepackages()
                if us:
                    ap2 = os.path.abspath(raw)
                    us2 = os.path.abspath(us)
                    nc_ap = os.path.normcase(ap2)
                    nc_us = os.path.normcase(us2)
                    out["from_user_site"] = nc_ap.startswith(nc_us + os.sep) or nc_ap == nc_us
        except Exception:
            pass
    except Exception:
        pass
    return out


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
    except Exception as e:
        key = "qwen_tts_import"
        if key in _selfcheck_import_warned:
            logger.debug("Qwen-tts self-check import failed (suppressed repeat): %s", e)
        else:
            _selfcheck_import_warned.add(key)
            logger.warning("Qwen-tts self-check import failed (often caused by broken torch): %s", e)
        return _user_pkg_has("qwen_tts")


def _torch_installed() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return _user_pkg_has("torch") or _pip_torch_artifacts_on_disk()
    except Exception as e:
        key = "torch_import"
        if key in _selfcheck_import_warned:
            logger.debug("Torch self-check import failed (suppressed repeat): %s", e)
        else:
            _selfcheck_import_warned.add(key)
            logger.warning(
                "Torch self-check import failed (possible mixed/half-uninstalled environment): %s", e
            )
        return _user_pkg_has("torch") or _pip_torch_artifacts_on_disk()


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
    """全套 AI 记忆依赖是否已安装（chromadb + transformers + sentence_transformers）。

    导入时可能因损坏的 torch（如混装、半卸载）抛出 RuntimeError 等，不可让 /api/setup 整页 500。
    """
    pkgs = ("chromadb", "transformers", "sentence_transformers")
    for mod in pkgs:
        try:
            __import__(mod)
        except ImportError:
            if not _user_pkg_has(mod):
                return False
        except Exception as e:
            logger.warning(
                "AI memory dependency self-check: importing %s failed (possibly broken torch or ABI issue); treat as not ready: %s",
                mod,
                e,
            )
            if _user_pkg_has(mod):
                continue
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


def build_setup_status_dict(config: Any) -> Dict[str, Any]:
    """与 GET /api/setup/status 相同载荷；供 Electron 启动器子进程（--setup-status-json）使用。"""
    import sysconfig

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
            "installed": _bundle_installed_on_disk(b),
            "recommended": b.get("recommended", False),
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

    _known_embed_dirs = [
        ("bge_small_zh",   "models/bge-small-zh-v1.5"),
        ("minilm_en",      "models/all-MiniLM-L6-v2"),
    ]
    embed_installed: dict = {}
    for eid, rel in _known_embed_dirs:
        fp = os.path.join(os.getcwd(), rel)
        embed_installed[eid] = _dir_looks_downloaded(fp)

    _stt_cfg_dict = stt_cfg if isinstance(stt_cfg, dict) else {}
    stt_ok, stt_reason = _stt_ready(_stt_cfg_dict)
    sv_cfg_path = (_stt_cfg_dict.get("model_path") or "").strip()
    sherpa_installed_dirs: dict = {}
    for rel in _SHERPA_SENSE_VOICE_SEARCH_DIRS:
        sherpa_installed_dirs[rel] = _dir_looks_downloaded(os.path.join(os.getcwd(), rel))

    with _download_lock:
        dl = dict(_download_state)

    _purge_stale_torch_modules_if_no_artifacts()
    _pip_install_clear_stale_errors()

    with _pip_install_lock:
        pip_st = dict(_pip_install_state)

    profile_dir = os.path.join(get_project_root(), "profiles")
    profile_count = 0
    if os.path.isdir(profile_dir):
        profile_count = len([x for x in os.listdir(profile_dir) if x.endswith(".json")])

    tts_st = _tts_status(config)
    torch_load = _torch_load_info()

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
        "torch_import_ok": _torch_import_ok(),
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
        "runtime_python": {
            "executable": sys.executable,
            "prefix": sys.prefix,
            "purelib": sysconfig.get_path("purelib"),
        },
        "torch_load": torch_load,
    }


@router.get("/status")
async def setup_status(request: Request) -> Dict[str, Any]:
    return build_setup_status_dict(request.app.state.config)


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


def _direct_url_archive_suffix(u: str) -> str:
    path = urlparse(u.split("?")[0]).path.lower()
    if path.endswith(".tar.bz2"):
        return ".tar.bz2"
    if path.endswith(".tar.gz"):
        return ".tar.gz"
    if path.endswith(".zip"):
        return ".zip"
    if path.endswith(".onnx"):
        return ".onnx"
    if path.endswith(".bin"):
        return ".bin"
    return ".bin"


def _download_worker_direct(bundle_id: str, url: str, local_dir: str) -> None:
    """直链下载：压缩包解压到 local_dir；.onnx / .bin 等单文件写入该目录。
    若主 URL 失败，自动尝试 GitHub 镜像代理。"""
    global _download_state
    import urllib.request
    import tarfile
    import zipfile
    import tempfile

    tmp_path = ""
    try:
        os.makedirs(local_dir, exist_ok=True)

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

        suffix = _direct_url_archive_suffix(actual_url)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.close()
        tmp_path = tmp.name

        # 流式下载，每 512KB 更新一次进度
        req = urllib.request.Request(actual_url, headers={"User-Agent": "shikigami/1.0"})
        with urllib.request.urlopen(req, timeout=600) as resp:
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

        bundles = _load_bundle_list()
        b = next((x for x in bundles if x.get("id") == bundle_id), None) or {}
        save_as = (b.get("direct_save_as") or "").strip()
        base_name = save_as or os.path.basename(urlparse(actual_url.split("?")[0]).path) or "download.bin"

        if suffix in (".tar.bz2", ".tar.gz", ".zip"):
            with _download_lock:
                _download_state["message"] = "解压中…"
                _download_state["progress_pct"] = 92
            if suffix in (".tar.bz2", ".tar.gz"):
                with tarfile.open(tmp_path, "r:*") as tf:
                    members = tf.getmembers()
                    top = members[0].name.split("/")[0] if members else ""
                    for m in members:
                        parts = m.name.split("/", 1)
                        rel = parts[1] if len(parts) > 1 and parts[0] == top else m.name
                        if not rel:
                            continue
                        m.name = rel
                        tf.extract(m, local_dir)
            else:
                with zipfile.ZipFile(tmp_path, "r") as zf:
                    zf.extractall(local_dir)
            os.unlink(tmp_path)
            tmp_path = ""
        else:
            with _download_lock:
                _download_state["message"] = "保存文件…"
                _download_state["progress_pct"] = 95
            os.makedirs(local_dir, exist_ok=True)
            dest = os.path.join(local_dir, base_name)
            try:
                if os.path.isfile(dest):
                    os.unlink(dest)
                shutil.move(tmp_path, dest)
            except OSError:
                shutil.copy2(tmp_path, dest)
                os.unlink(tmp_path)
            tmp_path = ""

        with _download_lock:
            _download_state["phase"] = "success"
            _download_state["progress_pct"] = 100
            _download_state["message"] = "完成"
            _download_state["error"] = None
    except Exception as e:
        logger.exception("[setup] direct download failed bundle=%s", bundle_id)
        if tmp_path:
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


def _parse_size_hint_bytes(size_hint: str) -> int:
    """将 '~3.4 GB' / '~400 MB' 解析为字节数，解析失败返回 0。"""
    import re
    m = re.search(r"([\d.]+)\s*(GB|MB|KB)", size_hint, re.IGNORECASE)
    if not m:
        return 0
    val, unit = float(m.group(1)), m.group(2).upper()
    mul = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}.get(unit, 1)
    return int(val * mul)


def _dir_size_bytes(path: str) -> int:
    total = 0
    try:
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _download_worker_modelscope(bundle_id: str, ms_repo_id: str, local_dir: str, size_hint: str = "") -> None:
    """用 ModelScope SDK 下载模型到 local_dir，后台轮询目录大小更新进度。"""
    global _download_state
    done_event = threading.Event()

    expected_bytes = _parse_size_hint_bytes(size_hint)

    def _progress_poller():
        while not done_event.is_set():
            done_event.wait(timeout=2)
            if done_event.is_set():
                break
            current = _dir_size_bytes(local_dir)
            with _download_lock:
                if _download_state["phase"] != "running":
                    break
                if expected_bytes > 0:
                    pct = min(95, int(current * 100 / expected_bytes))
                    # 只在有实际增长后才离开"正在连接"阶段
                    if pct > 5:
                        mb = current // 1024 // 1024
                        exp_mb = expected_bytes // 1024 // 1024
                        _download_state["progress_pct"] = pct
                        _download_state["message"] = f"下载中 {mb}MB / {exp_mb}MB"
                else:
                    current_mb = _dir_size_bytes(local_dir) // 1024 // 1024
                    if current_mb > 0:
                        _download_state["message"] = f"下载中 {current_mb}MB…"

    poller = threading.Thread(target=_progress_poller, daemon=True)

    try:
        os.makedirs(local_dir, exist_ok=True)
        from modelscope.hub.snapshot_download import snapshot_download as ms_snapshot_download

        with _download_lock:
            _download_state["message"] = "正在连接 ModelScope…"
            _download_state["progress_pct"] = 5

        poller.start()
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
    finally:
        done_event.set()


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
    size_hint = (b.get("size_hint") or "").strip()

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
                args=(body.bundle, ms_repo_id, local_dir, size_hint),
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
                args=(body.bundle, ms_repo_id, local_dir, size_hint),
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


def _emit_setup_event(obj: Dict[str, Any]) -> None:
    sys.stdout.write("SETUP_EVENT:" + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run_wizard_download_cli(bundle_id: str, source: str) -> int:
    """阻塞下载并向前端打印 SETUP_EVENT（供 Electron 启动器子进程）。"""
    bundles = _load_bundle_list()
    b = next((x for x in bundles if x.get("id") == bundle_id), None)
    if not b:
        _emit_setup_event({"type": "download", "phase": "error", "error": "unknown bundle", "bundle_id": bundle_id})
        return 1

    local_dir = _bundle_dir(b)
    direct_url = (b.get("direct_url") or "").strip()
    repo_id = (b.get("repo_id") or "").strip()
    ms_repo_id = (b.get("modelscope_repo_id") or "").strip()
    size_hint = (b.get("size_hint") or "").strip()
    source_used = source or "auto"
    thread: Optional[threading.Thread] = None

    if source_used == "direct":
        if not direct_url:
            _emit_setup_event({"type": "download", "phase": "error", "error": "no direct_url"})
            return 1
        thread = threading.Thread(
            target=_download_worker_direct,
            args=(bundle_id, direct_url, local_dir),
            daemon=True,
        )
    elif source_used == "hf":
        if not repo_id:
            _emit_setup_event({"type": "download", "phase": "error", "error": "no repo_id"})
            return 1
        thread = threading.Thread(
            target=_download_worker,
            args=(bundle_id, repo_id, local_dir),
            daemon=True,
        )
    elif source_used == "modelscope":
        if not ms_repo_id:
            _emit_setup_event({"type": "download", "phase": "error", "error": "no modelscope_repo_id"})
            return 1
        try:
            import modelscope  # noqa: F401
        except ModuleNotFoundError:
            _emit_setup_event({
                "type": "download",
                "phase": "error",
                "error": "未安装 modelscope 依赖",
                "code": "need_modelscope",
            })
            return 1
        thread = threading.Thread(
            target=_download_worker_modelscope,
            args=(bundle_id, ms_repo_id, local_dir, size_hint),
            daemon=True,
        )
    else:
        if direct_url:
            thread = threading.Thread(
                target=_download_worker_direct,
                args=(bundle_id, direct_url, local_dir),
                daemon=True,
            )
        elif repo_id:
            thread = threading.Thread(
                target=_download_worker,
                args=(bundle_id, repo_id, local_dir),
                daemon=True,
            )
        elif ms_repo_id:
            try:
                import modelscope  # noqa: F401
            except ModuleNotFoundError:
                _emit_setup_event({
                    "type": "download",
                    "phase": "error",
                    "error": "未安装 modelscope 依赖，且该模型包无 HF repo_id 可回退",
                })
                return 1
            thread = threading.Thread(
                target=_download_worker_modelscope,
                args=(bundle_id, ms_repo_id, local_dir, size_hint),
                daemon=True,
            )
        else:
            _emit_setup_event({"type": "download", "phase": "error", "error": "no download source"})
            return 1

    with _download_lock:
        if _download_state["phase"] == "running":
            _emit_setup_event({"type": "download", "phase": "error", "error": "已有任务进行中"})
            return 1
        _download_state["phase"] = "running"
        _download_state["bundle_id"] = bundle_id
        _download_state["progress_pct"] = 0
        _download_state["message"] = "启动…"
        _download_state["error"] = None

    assert thread is not None
    thread.start()

    while thread.is_alive():
        with _download_lock:
            st = dict(_download_state)
        st["type"] = "download"
        _emit_setup_event(st)
        time.sleep(0.35)

    with _download_lock:
        st = dict(_download_state)
    st["type"] = "download"
    _emit_setup_event(st)
    return 0 if st.get("phase") == "success" else 1


def wizard_apply_stt_model_cli(model_path: str) -> int:
    from src.api.settings_ext import _load_yaml, _save_yaml

    path = (model_path or "").strip()
    y, data = _load_yaml()
    if "stt" not in data:
        data["stt"] = {}
    data["stt"]["model_path"] = path
    _save_yaml(y, data)
    return 0


def wizard_launch_gptsovits_cli(config: Any) -> int:
    import platform

    is_win = platform.system() == "Windows"
    script = "scripts/start_gptsovits.bat" if is_win else "scripts/start_gptsovits.sh"
    if not os.path.isfile(script):
        return 1
    script_abs = os.path.abspath(script)
    env = os.environ.copy()
    try:
        gpt_dir = (config.tts_config.get("gpt_sovits") or {}).get("dir", "").strip()
        if gpt_dir:
            env["GPTSOVITS_DIR"] = os.path.abspath(gpt_dir)
    except Exception:
        pass
    try:
        if is_win:
            subprocess.Popen(["cmd", "/c", "start", "", script_abs], shell=False, cwd=os.getcwd(), env=env)
        else:
            subprocess.Popen(
                ["bash", script_abs],
                cwd=os.getcwd(),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return 0
    except OSError:
        return 1


def run_wizard_pip_uninstall_cli(packages: List[str], dirs: List[str]) -> int:
    r = run_pip_uninstall_sync(packages, dirs)
    sys.stdout.write("SETUP_RESULT:" + json.dumps(r, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if r.get("ok") else 1


# ─── Pip install / uninstall ─────────────────────────────────────────────────

class PipInstallBody(BaseModel):
    packages: List[str]
    target: str = ""   # 前端传入，用于区分哪个按钮触发了安装
    index_url: str = ""  # 可选，如 https://download.pytorch.org/whl/cu124


class PipUninstallBody(BaseModel):
    packages: List[str]   # pip 包名列表
    dirs: List[str] = []  # 额外要删除的目录（相对于项目根，如 models/all-MiniLM-L6-v2）


def _pip_spec_base(spec: str) -> str:
    """去掉版本约束与 extras，得到 pip 包名片段（小写）。"""
    s = (spec or "").strip().split(";")[0].strip()
    for op in (">=", "==", "<=", "!=", "~=", ">", "<"):
        if op in s:
            s = s.split(op)[0].strip()
            break
    return s.split("[")[0].strip().lower()


def _user_entry_matches_installed_pkg(entry: str, pkg_base: str) -> bool:
    """判断 user_packages 下顶层项是否属于包 pkg_base（用下划线规范名，如 qwen_tts、torch）。

    避免 torch 误匹配 torchgen：torchgen 不等于 torch，也不以 torch- 开头。
    """
    pb = pkg_base.lower().replace("-", "_")
    el = entry.lower()
    if el == pb:
        return True
    pb_hy = pb.replace("_", "-")
    if el == pb_hy:
        return True
    if el.endswith(".dist-info"):
        stem = el[: -len(".dist-info")]
        if stem.startswith(pb + "-") or stem.startswith(pb_hy + "-"):
            return True
        stem_us = stem.replace("-", "_")
        if stem_us.startswith(pb + "-"):
            return True
    return False


def _maybe_torch_dll_winerror5_payload(exc: BaseException, path: str) -> Optional[Dict[str, str]]:
    """If WinError 5 on torch .dll/.pyd, return an i18n key for the frontend (see i18n.js setupPip*)."""
    if not sys.platform.startswith("win"):
        return None
    if getattr(exc, "winerror", None) != 5:
        return None
    blob = f"{path} {exc}".lower()
    if "torch" not in blob:
        return None
    if ".dll" not in blob and ".pyd" not in blob:
        return None
    key = (
        "setupPipTorchDllLockedPacked"
        if "user_packages" in blob
        else "setupPipTorchDllLockedVenv"
    )
    return {"key": key, "detail": str(exc)}


def _append_fs_removal_error(errors: list, top_name: str, err: Union[str, Dict[str, Any], None]) -> None:
    if not err:
        return
    if isinstance(err, dict):
        row = dict(err)
        row["prefix"] = top_name
        errors.append(row)
    else:
        errors.append(f"{top_name}: {err}")


def _pip_uninstall_fs_error_covers_pkg(fs_errors: list, pkg_base: str) -> bool:
    """True if fs removal already reported WinError / access denied for this package (avoid duplicate residual lines)."""
    pb = (pkg_base or "").lower()
    if not pb:
        return False
    for e in fs_errors:
        if isinstance(e, dict):
            pref = (e.get("prefix") or "").lower()
            if pref != pb:
                continue
            k = str(e.get("key") or "")
            if k.startswith("setupPipTorchDllLocked"):
                return True
            det = str(e.get("detail") or "").lower()
            if "winerror 5" in det or "access is denied" in det:
                return True
        elif isinstance(e, str):
            el = e.lower()
            if not el.startswith(pb + ":"):
                continue
            if "winerror 5" in el or "access is denied" in el:
                return True
    return False


def _path_make_writable(path: str) -> None:
    try:
        import stat

        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass


def _tree_make_writable(root: str) -> None:
    if not os.path.isdir(root):
        _path_make_writable(root)
        return
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        for fn in filenames:
            _path_make_writable(os.path.join(dirpath, fn))
        _path_make_writable(dirpath)
    _path_make_writable(root)


def _unlink_best_effort(path: str) -> Union[None, str, Dict[str, Any]]:
    import gc

    if not os.path.lexists(path):
        return None
    for attempt in range(3):
        try:
            _path_make_writable(path)
            os.remove(path)
            return None
        except OSError as e:
            if attempt < 2 and getattr(e, "winerror", None) == 5:
                gc.collect()
                time.sleep(0.25)
                continue
            pl = _maybe_torch_dll_winerror5_payload(e, path)
            if pl is not None:
                return pl
            return str(e)
    return "unlink failed"


def _rmtree_best_effort(path: str) -> Union[None, str, Dict[str, Any]]:
    """Windows: clearance of read-only bits + retries for transient WinError 5 on ``__pycache__`` / DLL paths."""
    import gc
    import shutil

    if not os.path.lexists(path):
        return None
    if not os.path.isdir(path):
        return _unlink_best_effort(path)
    for attempt in range(3):
        try:
            _tree_make_writable(path)
            if sys.version_info >= (3, 12):

                def _onexc(func, p, exc):
                    _path_make_writable(p)
                    func(p)

                shutil.rmtree(path, onexc=_onexc)
            else:

                def _onerror(func, p, exc_info):
                    _path_make_writable(p)
                    func(p)

                shutil.rmtree(path, onerror=_onerror)
            return None
        except OSError as e:
            if attempt < 2 and getattr(e, "winerror", None) == 5:
                gc.collect()
                time.sleep(0.25)
                continue
            pl = _maybe_torch_dll_winerror5_payload(e, f"{path} {e}")
            if pl is not None:
                return pl
            return str(e)
    return "rmtree failed"


def _remove_frozen_target_packages(target_dir: str, package_specs: List[str]) -> List[Any]:
    """Frozen 安装使用 pip install --target；pip uninstall --target 通常无效，直接按目录删除。"""
    errors: list = []
    if not os.path.isdir(target_dir):
        return errors
    bases = {_pip_spec_base(p).replace("-", "_") for p in package_specs if (p or "").strip()}
    try:
        entries = os.listdir(target_dir)
    except OSError as e:
        return [str(e)]
    to_remove: List[str] = []
    for e in entries:
        for pb in bases:
            if _user_entry_matches_installed_pkg(e, pb):
                to_remove.append(os.path.join(target_dir, e))
                break
    for full in to_remove:
        top = os.path.basename(full)
        if os.path.isdir(full):
            _append_fs_removal_error(errors, top, _rmtree_best_effort(full))
        else:
            _append_fs_removal_error(errors, top, _unlink_best_effort(full))
    return errors


def _remove_source_site_packages_top_level(package_specs: List[str]) -> List[Any]:
    """Remove package files directly from this interpreter's site-packages roots.

    Purpose:
    - When a previous uninstall/upgrade got interrupted, ``pip uninstall`` may say "not installed"
      even though the package still exists as importable files (missing/broken dist-info).
    - In that case, filesystem deletion is the most reliable way to make /api/setup/status update.

    Safety:
    - We only delete the exact top-level package dir name (e.g. ``torch/``)
      and matching ``<name>-*.dist-info`` entries.
    """
    errors: List[Any] = []
    roots = _torch_scan_roots()
    if not roots:
        return errors

    bases = []
    for spec in package_specs:
        pb = _pip_spec_base(spec).lower()
        if not pb:
            continue
        safe = pb.replace("-", "_")
        bases.append((safe, safe.replace("_", "-")))

    to_remove: List[str] = []
    for root in roots:
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for e in entries:
            el = e.lower()
            for safe_us, safe_dash in bases:
                # top-level package directory (torch/, torchvision/, torchaudio/)
                if el == safe_us:
                    to_remove.append(os.path.join(root, e))
                    break
                # dist-info entries (torch-2.x.y+cpu.dist-info)
                if el.endswith(".dist-info") and (
                    el.startswith(safe_us + "-") or el.startswith(safe_dash + "-")
                ):
                    to_remove.append(os.path.join(root, e))
                    break

    # Deduplicate while preserving order
    seen: set[str] = set()
    uniq: List[str] = []
    for p in to_remove:
        if p not in seen:
            seen.add(p)
            uniq.append(p)

    for full in uniq:
        top = os.path.basename(full)
        if os.path.isdir(full):
            _append_fs_removal_error(errors, top, _rmtree_best_effort(full))
        else:
            _append_fs_removal_error(errors, top, _unlink_best_effort(full))

    return errors


def _clear_sys_modules_for_packages(package_specs: List[str]) -> None:
    """卸载后清掉已加载模块，否则同进程内 import 仍成功，自检一直显示已安装。"""
    import importlib

    roots = {_pip_spec_base(p).replace("-", "_") for p in package_specs if (p or "").strip()}
    to_del = [
        k
        for k in list(sys.modules)
        if k.split(".")[0] in roots
    ]
    for k in to_del:
        try:
            del sys.modules[k]
        except KeyError:
            pass
    importlib.invalidate_caches()


def _frozen_pkg_files_still_present(target_dir: str, pkg_spec: str) -> bool:
    if not os.path.isdir(target_dir):
        return False
    pb = _pip_spec_base(pkg_spec).replace("-", "_")
    try:
        for e in os.listdir(target_dir):
            if _user_entry_matches_installed_pkg(e, pb):
                return True
    except OSError:
        return False
    return False


def _pip_error_message(
    output: str,
    packages: List[str],
    index_url: str,
    python_exe: str = "",
    target_dir: str = "",
    install_target: str = "",
) -> Union[str, Dict[str, Any]]:
    """Map pip stderr to an i18n key (frontend) plus optional raw tail for support.

    ModelScope triggers the same WinError5 path as Qwen when pip upgrades torch: the *parent*
    server process may already have torch DLLs mapped from ``user_packages``/site-packages while
    the pip *subprocess* tries to replace those files.
    """
    low_out = (output or "").lower()
    if "winerror 5" in low_out or "access is denied" in low_out:
        tgt = (install_target or "").strip().lower()
        tail = (output or "").strip()
        if len(tail) > 1200:
            tail = tail[-1200:]
        if tgt == "modelscope":
            return {"key": "setupPipWinerror5Modelscope", "detail": tail}
        if tgt in ("qwen_tts", "torch_cuda"):
            return {"key": "setupPipWinerror5QwenTorch", "detail": tail}
        return {"key": "setupPipWinerror5Generic", "detail": tail}
    return output[-800:]


def _pip_install_worker(packages: List[str], index_url: str = "", install_target: str = "") -> None:
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
                    _pip_install_state["error"] = "Python interpreter not found"
                return
            target_dir = os.path.join(exe_dir, "user_packages")
            os.makedirs(target_dir, exist_ok=True)
            cmd = [embed_python, "-m", "pip", "install", "--upgrade", "--target", target_dir] + packages
            if index_url:
                cmd += ["--index-url", index_url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
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
                    raw = result.stderr or result.stdout or ""
                    _pip_install_state["phase"] = "error"
                    _pip_install_state["message"] = "安装失败"
                    _pip_install_state["error"] = _pip_error_message(
                        raw, packages, index_url, embed_python, target_dir, install_target
                    )
            return

        cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + packages
        if index_url:
            cmd += ["--index-url", index_url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode == 0:
            import importlib
            importlib.invalidate_caches()
        with _pip_install_lock:
            if result.returncode == 0:
                _pip_install_state["phase"] = "success"
                _pip_install_state["message"] = "安装完成"
                _pip_install_state["error"] = None
            else:
                raw = result.stderr or result.stdout or ""
                _pip_install_state["phase"] = "error"
                _pip_install_state["message"] = "安装失败"
                _pip_install_state["error"] = _pip_error_message(
                    raw, packages, index_url, install_target=install_target
                )
    except Exception as e:
        with _pip_install_lock:
            _pip_install_state["phase"] = "error"
            _pip_install_state["message"] = "安装失败"
            _pip_install_state["error"] = str(e)[:500]


@router.post("/pip-install")
async def pip_install(body: PipInstallBody) -> Dict[str, Any]:
    with _pip_install_lock:
        if _pip_install_state["phase"] == "running":
            return JSONResponse(status_code=409, content={"ok": False, "error": "An installation task is already running"})
        _pip_install_state["phase"] = "running"
        _pip_install_state["target"] = body.target or ""
        _pip_install_state["message"] = "启动…"
        _pip_install_state["error"] = None
    thread = threading.Thread(
        target=_pip_install_worker,
        args=(list(body.packages), body.index_url or "", body.target or ""),
        daemon=True,
    )
    thread.start()
    return {"ok": True}


@router.get("/pip-install/status")
async def pip_install_status() -> Dict[str, Any]:
    with _pip_install_lock:
        return dict(_pip_install_state)


def run_pip_uninstall_sync(packages: List[str], dirs: List[str]) -> Dict[str, Any]:
    """与 POST /api/setup/pip-uninstall 相同逻辑；供启动器子进程与 HTTP 共用。"""
    import gc
    import shutil as _shutil
    from src.utils.paths import get_project_root

    errors: list = []
    names = [_pip_spec_base(p) for p in (packages or []) if (p or "").strip()]

    if names:
        if getattr(sys, "frozen", False):
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            target_dir = os.path.join(exe_dir, "user_packages")
            _clear_sys_modules_for_packages(packages)
            gc.collect()
            fs_err = _remove_frozen_target_packages(target_dir, packages)
            errors.extend(fs_err)
            embed_python = os.path.join(exe_dir, "python_embed", "python.exe")
            if not os.path.isfile(embed_python):
                embed_python = _shutil.which("python3") or _shutil.which("python") or ""
            if embed_python:
                cmd = [embed_python, "-m", "pip", "uninstall", "-y"] + names
                subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            _clear_sys_modules_for_packages(packages)
            for spec in packages:
                if _frozen_pkg_files_still_present(target_dir, spec):
                    pb = _pip_spec_base(spec)
                    if _pip_uninstall_fs_error_covers_pkg(fs_err, pb):
                        continue
                    errors.append(
                        {"key": "setupPipUninstallResidual", "pkg": pb, "dir": target_dir}
                    )
        else:
            _clear_sys_modules_for_packages(packages)
            gc.collect()
            fs_err = _remove_source_site_packages_top_level(packages)
            errors.extend(fs_err)
            cmd = [sys.executable, "-m", "pip", "uninstall", "-y"] + names
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            _clear_sys_modules_for_packages(packages)
            if result.returncode != 0:
                tail = (result.stderr or result.stdout or "")[-500:]
                errors.append(tail or "pip uninstall failed")

    root = get_project_root()
    for rel_dir in (dirs or []):
        abs_dir = os.path.normpath(os.path.join(root, rel_dir))
        if not abs_dir.startswith(os.path.join(root, "models")):
            errors.append(f"Refused to delete non-models directory: {rel_dir}")
            continue
        if os.path.isdir(abs_dir):
            try:
                _shutil.rmtree(abs_dir)
            except Exception as e:
                errors.append(f"Failed to delete {rel_dir}: {e}")

    import importlib
    importlib.invalidate_caches()

    if errors:
        return {"ok": False, "errors": errors}
    return {"ok": True}


@router.post("/pip-uninstall")
async def pip_uninstall(body: PipUninstallBody) -> Dict[str, Any]:
    """Uninstall pip packages and optionally remove model dirs (synchronous).

    - **Source / .venv**: remove matching site-packages trees first (covers broken dist-info), then
      ``python -m pip uninstall -y``. If WinError 5 persists, torch native DLLs are likely still
      mapped—fully quit the app and uninstall again (or delete/recreate the venv).
    - **Frozen (packaged exe)**: wheels are installed with ``pip install --target user_packages``.
      A plain ``pip uninstall`` often does not touch that directory or has no uninstall record there,
      so we remove matching top-level folders / ``*.dist-info`` under ``user_packages`` first, then
      run pip as a best-effort cleanup. Also clears ``sys.modules`` so the UI does not still see imports.
    """
    return run_pip_uninstall_sync(list(body.packages or []), list(body.dirs or []))


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


