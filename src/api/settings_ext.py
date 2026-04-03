"""
settings_ext.py — Extended settings endpoints (P5-A)

Provides persistent CRUD for:
  • LLM presets (GET/POST/PUT/DELETE + set-active)
  • TTS config (voices list + save)
  • Profiles (GET/POST/PUT/DELETE) — each profile is bound 1:1 to a session
  • System config (log_level, max_history_turns)

All writes use ruamel.yaml to preserve YAML comments in config/app.yaml.
Profile CRUD operates directly on profiles/*.json files and syncs SessionManager.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.config.emotion_keys import emotion_keys_csv_for_wizard
from src.llm.registry import get_provider
from src.lorebooks.entry_utils import normalize_lorebook_entry_for_storage
from src.utils.paths import get_project_root

_STATIC_AVATARS_DIR = "static/avatars"

logger = logging.getLogger(__name__)
router = APIRouter()

_YAML_PATH = os.path.join(get_project_root(), "config", "app.yaml")
_ENV_FILE_PATH = os.path.join(get_project_root(), ".env")
_PROFILES_DIR = os.path.join(get_project_root(), "profiles")


@router.get("/settings/emotion_keys")
async def get_emotion_keys(request: Request):
    """Ordered emotion keys for settings UI ."""
    from src.config.emotion_keys import emotion_keys_for_api

    public_mode = True  # [emotion_keys]
    return {"keys": emotion_keys_for_api(public_mode=public_mode)}


# ─────────────────────────────────────────────────────────────────────────────
# YAML helpers (ruamel preserves comments)
# ─────────────────────────────────────────────────────────────────────────────

def _load_yaml():
    from ruamel.yaml import YAML
    y = YAML()
    y.preserve_quotes = True
    if not os.path.exists(_YAML_PATH):
        return y, {}
    with open(_YAML_PATH, "r", encoding="utf-8") as f:
        return y, y.load(f)


def _save_yaml(y, data):
    os.makedirs(os.path.dirname(_YAML_PATH), exist_ok=True)
    with open(_YAML_PATH, "w", encoding="utf-8") as f:
        y.dump(data, f)


_ENV_FILE = _ENV_FILE_PATH


def _env_key_name(preset_name: str) -> str:
    """Return the .env variable name for a given preset (matches app_config.py convention)."""
    return f"{preset_name.upper().replace(' ', '_')}_API_KEY"


def _ensure_bg_loops(app, session) -> None:
    """Start ASE (and reflection if needed) background loops for a session
    that was loaded after server startup. Safe to call on already-running sessions."""
    if session is None:
        return
    ase_engine = getattr(app.state, "ase_engine", None)
    if ase_engine and hasattr(ase_engine, "_start_session_loop"):
        ase_cfg = getattr(app.state, "config", None)
        if ase_cfg:
            ase_cfg = ase_cfg.get_ase_config() if hasattr(ase_cfg, "get_ase_config") else {}
        if ase_cfg and ase_cfg.get("enabled"):
            ase_engine._start_session_loop(session)


def _write_key_to_env(preset_name: str, api_key: str) -> None:
    """Write api_key to .env and os.environ. If empty, remove from both."""
    from dotenv import set_key, unset_key
    env_key = _env_key_name(preset_name)
    if api_key:
        set_key(_ENV_FILE, env_key, api_key)
        os.environ[env_key] = api_key
    else:
        unset_key(_ENV_FILE, env_key)
        os.environ.pop(env_key, None)


# ─────────────────────────────────────────────────────────────────────────────
# LLM Presets
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/settings/llm/presets")
async def get_llm_presets(request: Request):
    """Return all LLM presets.  API keys are never returned — only has_key bool."""
    config = request.app.state.config
    result = {}
    for name, p in config.llm_presets.items():
        result[name] = {
            "type":                   p.get("type", "openai_compat"),
            "base_url":               p.get("base_url", ""),
            "model":                  p.get("model", ""),
            "has_key":                bool(p.get("api_key", "")),
            "temperature":            p.get("temperature", 1.0),
            "top_p":                  p.get("top_p", 0.95),
            "presence_penalty":       p.get("presence_penalty", 0.0),
            "frequency_penalty":      p.get("frequency_penalty", 0.0),
            "unsupported_params":     p.get("unsupported_params", []),
        }
    return {
        "presets": result,
        "active":  config.default_llm,
    }


class LLMPresetBody(BaseModel):
    name: str
    type: str = "openai_compat"
    api_key: Optional[str] = None          # None = keep existing; "" = clear
    base_url: str = ""
    model: str = ""
    temperature: float = 1.0
    top_p: float = 0.95
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0
    unsupported_params: List[str] = []


@router.post("/settings/llm/presets")
async def create_llm_preset(request: Request, body: LLMPresetBody):
    """Create a new LLM preset and persist it to app.yaml."""
    config = request.app.state.config
    if body.name in config.llm_presets:
        raise HTTPException(status_code=409, detail=f"Preset '{body.name}' already exists")

    y, data = _load_yaml()
    if "llm_presets" not in data:
        data["llm_presets"] = {}

    # Write api_key to .env (source of truth); YAML only stores empty string
    final_api_key = body.api_key or ""
    _write_key_to_env(body.name, final_api_key)

    preset_data: Dict[str, Any] = {
        "type":                  body.type,
        "api_key":               "",   # key lives in .env, not YAML
        "base_url":              body.base_url,
        "model":                 body.model,
        "temperature":           body.temperature,
        "top_p":                 body.top_p,
    }
    if body.presence_penalty != 0.0:
        preset_data["presence_penalty"] = body.presence_penalty
    if body.frequency_penalty != 0.0:
        preset_data["frequency_penalty"] = body.frequency_penalty
    if body.unsupported_params:
        preset_data["unsupported_params"] = body.unsupported_params

    data["llm_presets"][body.name] = preset_data
    _save_yaml(y, data)

    # Hot-reload: keep actual key in memory (not the empty YAML value)
    config.llm_presets[body.name] = dict(preset_data)
    config.llm_presets[body.name]["api_key"] = final_api_key

    # Re-register analysis provider in case this preset is used for analysis
    from src.llm.registry import ensure_analysis_provider
    ensure_analysis_provider(config)

    return {"ok": True, "name": body.name}


@router.put("/settings/llm/presets/{name}")
async def update_llm_preset(name: str, request: Request, body: LLMPresetBody):
    """Update an existing LLM preset and persist. Hot-reloads immediately."""
    config = request.app.state.config

    y, data = _load_yaml()
    presets = data.get("llm_presets", {})

    # Determine final api_key: None means keep existing; empty string means clear
    existing_key = ""
    if name in config.llm_presets:
        existing_key = config.llm_presets[name].get("api_key", "")
    final_key = existing_key if body.api_key is None else body.api_key

    old_name = name  # remember original name for env var migration

    # Handle rename: old name → new name
    if body.name != name:
        if body.name in config.llm_presets:
            raise HTTPException(status_code=409, detail=f"Preset '{body.name}' already exists")
        # Move in YAML
        old_entry = presets.pop(name, {})
        presets[body.name] = old_entry
        # Move in memory
        config.llm_presets.pop(name, None)
        # If this was the active preset, update default_llm
        if config.default_llm == name:
            config.default_llm = body.name
            data["default_llm"] = body.name
        name = body.name

    # Migrate api_key to .env (remove old env var on rename, set new one)
    if old_name != name:
        _write_key_to_env(old_name, "")   # clear old env var
    _write_key_to_env(name, final_key)    # set (or clear if "") new env var

    preset_data: Dict[str, Any] = {
        "type":                  body.type,
        "api_key":               "",   # key lives in .env, not YAML
        "base_url":              body.base_url,
        "model":                 body.model,
        "temperature":           body.temperature,
        "top_p":                 body.top_p,
    }
    # Only include penalty params if not in unsupported
    if "presence_penalty" not in body.unsupported_params:
        preset_data["presence_penalty"] = body.presence_penalty
    if "frequency_penalty" not in body.unsupported_params:
        preset_data["frequency_penalty"] = body.frequency_penalty
    if body.unsupported_params:
        preset_data["unsupported_params"] = body.unsupported_params

    if "llm_presets" not in data:
        data["llm_presets"] = {}
    data["llm_presets"][name] = preset_data
    _save_yaml(y, data)

    # Hot-reload: keep actual key in memory (not the empty YAML value)
    config.llm_presets[name] = dict(preset_data)
    config.llm_presets[name]["api_key"] = final_key

    # Re-register analysis provider in case this preset is used for analysis
    from src.llm.registry import ensure_analysis_provider
    ensure_analysis_provider(config)

    return {"ok": True, "name": name}


@router.delete("/settings/llm/presets/{name}")
async def delete_llm_preset(name: str, request: Request):
    """Delete a preset. Refuses if it is currently the active preset."""
    config = request.app.state.config
    if config.default_llm == name:
        raise HTTPException(status_code=400, detail="Cannot delete the active preset. Switch to another preset first.")

    y, data = _load_yaml()
    presets = data.get("llm_presets", {})
    if name not in presets:
        raise HTTPException(status_code=404, detail=f"Preset '{name}' not found")

    del presets[name]
    _save_yaml(y, data)
    config.llm_presets.pop(name, None)
    _write_key_to_env(name, "")   # remove from .env and os.environ
    return {"ok": True}


class SetActiveBody(BaseModel):
    name: str


@router.post("/settings/llm/active")
async def set_active_llm(request: Request, body: SetActiveBody):
    """Set the active LLM preset. Takes effect immediately for the next chat request."""
    config = request.app.state.config
    if body.name not in config.llm_presets:
        raise HTTPException(status_code=404, detail=f"Preset '{body.name}' not found")

    config.default_llm = body.name

    # Persist to YAML
    y, data = _load_yaml()
    data["default_llm"] = body.name
    _save_yaml(y, data)

    # Re-register analysis provider in case it uses the active model (preset = "")
    from src.llm.registry import ensure_analysis_provider
    ensure_analysis_provider(config)

    return {"ok": True, "active": body.name}


@router.post("/settings/llm/presets/{name}/test")
async def test_llm_preset(name: str, request: Request):
    """测试指定 LLM 预设是否可用，并返回响应时间（毫秒）。"""
    config = request.app.state.config
    preset = config.llm_presets.get(name)
    if not preset:
        raise HTTPException(status_code=404, detail=f"Preset '{name}' not found")
    try:
        provider = get_provider(preset)
        t0 = time.perf_counter()
        out = await provider.chat([{"role": "user", "content": "Hi"}])
        latency_ms = (time.perf_counter() - t0) * 1000
        if out and out.strip().startswith("[LLM Error:"):
            return JSONResponse(status_code=503, content={"ok": False, "error": out.strip("[]")})
        return {"ok": True, "latency_ms": round(latency_ms, 1)}
    except Exception as e:
        return JSONResponse(status_code=503, content={"ok": False, "error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# TTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/settings/tts/voices")
async def get_tts_voices(language: str = "zh-CN"):
    """Return a filtered list of Edge TTS voices for the given language."""
    try:
        import edge_tts
        voices_raw = await edge_tts.list_voices()
        voices = [
            {"value": v["ShortName"], "label": f"{v['ShortName']} ({v.get('Gender', '')})"}
            for v in voices_raw
            if v.get("Locale", "").startswith(language.split("-")[0])
        ]
        return {"voices": voices}
    except Exception as e:
        logger.warning(f"[settings_ext] tts voices error: {e}")
        return {"voices": []}


_KOKORO_VOICE_GROUPS = {
    "Chinese Female": ["zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi"],
    "Chinese Male":   ["zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang"],
    "Japanese Female":["jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro"],
    "Japanese Male":  ["jm_kumo"],
    "American Female":["af_alloy","af_aoede","af_bella","af_heart","af_jessica","af_kore","af_nicole","af_nova","af_river","af_sarah","af_sky"],
    "American Male":  ["am_adam","am_echo","am_eric","am_fenrir","am_liam","am_michael","am_onyx","am_puck","am_santa"],
    "British Female": ["bf_alice","bf_emma","bf_isabella","bf_lily"],
    "British Male":   ["bm_daniel","bm_fable","bm_george","bm_lewis"],
    "Other":          ["ff_siwis","ef_dora","em_alex","em_santa","hf_alpha","hf_beta","hm_omega","hm_psi","if_sara","im_nicola","pf_dora","pm_alex","pm_santa"],
}

_KOKORO_PROJECT_ROOT = get_project_root()
_KOKORO_VOICES_CANDIDATES = ["voices-v1.0.bin", "voices-v0_19.bin"]


def _get_kokoro_voice_groups() -> dict:
    """Try to load actual voice keys from the voices.bin file; fall back to hardcoded list."""
    search_dirs = [os.path.join(_KOKORO_PROJECT_ROOT, "models"), _KOKORO_PROJECT_ROOT]
    for d in search_dirs:
        for fname in _KOKORO_VOICES_CANDIDATES:
            p = os.path.join(d, fname)
            if not os.path.isfile(p):
                continue
            try:
                import numpy as np
                data = np.load(p, allow_pickle=True)
                keys = list(data.keys())
                if keys:
                    # Build groups from actual keys using prefix matching
                    groups: dict = {}
                    for group_name, prefixes_samples in [
                        ("Chinese Female",  ["zf_"]),
                        ("Chinese Male",    ["zm_"]),
                        ("Japanese Female", ["jf_"]),
                        ("Japanese Male",   ["jm_"]),
                        ("American Female", ["af_"]),
                        ("American Male",   ["am_"]),
                        ("British Female",  ["bf_"]),
                        ("British Male",    ["bm_"]),
                    ]:
                        matched = [k for k in keys if any(k.startswith(px) for px in prefixes_samples)]
                        if matched:
                            groups[group_name] = sorted(matched)
                    others = [k for k in keys if not any(
                        k.startswith(px)
                        for px in ["zf_","zm_","jf_","jm_","af_","am_","bf_","bm_"]
                    )]
                    if others:
                        groups["Other"] = sorted(others)
                    return groups
            except Exception:
                pass
    return _KOKORO_VOICE_GROUPS


@router.get("/settings/tts/kokoro/voices")
async def get_kokoro_voices():
    """Return grouped Kokoro voice list; reads actual keys from voices.bin if available."""
    groups = _get_kokoro_voice_groups()
    all_voices = [v for vlist in groups.values() for v in vlist]
    return {"voices": all_voices, "groups": groups}


class TTSSaveBody(BaseModel):
    engine: str = "edge_tts"
    # Edge TTS fields
    voice: str = "zh-CN-XiaoxiaoNeural"
    rate_pct: int = 0   # -50 to +100 (percent offset)
    language: str = "zh-CN"
    # GPT-SoVITS fields
    gptsovits_host: str = "127.0.0.1"
    gptsovits_port: int = 9880
    gptsovits_dir: str = ""
    gptsovits_text_lang: str = "zh"
    gptsovits_prompt_lang: str = "zh"
    gptsovits_speed: float = 1.0
    gptsovits_temperature: float = 1.0
    gptsovits_top_p: float = 1.0
    gptsovits_top_k: int = 15
    gptsovits_repetition_penalty: float = 1.35
    # GPT-SoVITS default reference audio (global fallback for profiles without per-profile config)
    gptsovits_ref_audio_path: str = ""
    gptsovits_prompt_text: str = ""
    # Qwen3-TTS default (female Vivian when no profile override)
    qwen3_mode: str = "custom_voice"
    qwen3_model_id: str = ""
    qwen3_device: str = "cuda:0"
    qwen3_dtype: str = "bfloat16"
    qwen3_language: str = "Chinese"
    qwen3_speaker: str = "Vivian"
    qwen3_instruct: str = ""
    qwen3_voice_description: str = ""
    qwen3_ref_audio_path: str = ""
    qwen3_ref_text: str = ""
    qwen3_temperature: float = 0.9
    qwen3_top_p: float = 1.0
    qwen3_top_k: int = 50
    qwen3_repetition_penalty: float = 1.05
    qwen3_attn_implementation: str = "eager"
    qwen3_use_torch_compile: bool = False
    qwen3_use_sentence_chunking: bool = False
    qwen3_sentence_max_chars: int = 0
    # KokoroTTS fields
    kokoro_voice: str = ""
    kokoro_lang: str = "zh"
    kokoro_speed: float = 1.0
    kokoro_auto_detect_lang: bool = True


@router.get("/settings/tts/config")
async def get_tts_config(request: Request):
    """Return the current TTS engine + full config for the settings UI."""
    config = request.app.state.config
    return {
        "engine": config.default_tts,
        "edge_tts": config.tts_config.get("edge_tts", {}),
        "gpt_sovits": config.tts_config.get("gpt_sovits", {}),
        "qwen3_tts": config.tts_config.get("qwen3_tts", {}),
        "kokoro": config.tts_config.get("kokoro", {}),
    }


@router.post("/settings/tts/save")
async def save_tts_config(request: Request, body: TTSSaveBody):
    config = request.app.state.config
    rate_str = f"{'+' if body.rate_pct >= 0 else ''}{body.rate_pct}%"

    y, data = _load_yaml()
    if "tts" not in data:
        data["tts"] = {}

    # ── Edge TTS ──────────────────────────────────────────────────────────────
    if "edge_tts" not in data["tts"]:
        data["tts"]["edge_tts"] = {}
    data["tts"]["edge_tts"]["voice"] = body.voice
    data["tts"]["edge_tts"]["rate"] = rate_str

    # ── GPT-SoVITS ────────────────────────────────────────────────────────────
    if "gpt_sovits" not in data["tts"]:
        data["tts"]["gpt_sovits"] = {}
    gs = data["tts"]["gpt_sovits"]
    gs["host"] = body.gptsovits_host
    gs["port"] = body.gptsovits_port
    gs["dir"] = body.gptsovits_dir
    gs["text_lang"] = body.gptsovits_text_lang
    gs["prompt_lang"] = body.gptsovits_prompt_lang
    gs["speed_factor"] = body.gptsovits_speed
    gs["temperature"] = body.gptsovits_temperature
    gs["top_p"] = body.gptsovits_top_p
    gs["top_k"] = body.gptsovits_top_k
    gs["repetition_penalty"] = body.gptsovits_repetition_penalty
    gs["ref_audio_path"] = body.gptsovits_ref_audio_path
    gs["prompt_text"] = body.gptsovits_prompt_text

    # ── Qwen3-TTS ─────────────────────────────────────────────────────────────
    if "qwen3_tts" not in data["tts"]:
        data["tts"]["qwen3_tts"] = {}
    q3 = data["tts"]["qwen3_tts"]
    q3["mode"] = getattr(body, "qwen3_mode", "custom_voice")
    q3["model_id"] = getattr(body, "qwen3_model_id", "")
    q3["device"] = getattr(body, "qwen3_device", "cuda:0")
    q3["dtype"] = getattr(body, "qwen3_dtype", "bfloat16")
    q3["language"] = getattr(body, "qwen3_language", "Chinese")
    q3["speaker"] = getattr(body, "qwen3_speaker", "Vivian")
    q3["instruct"] = getattr(body, "qwen3_instruct", "")
    q3["voice_description"] = getattr(body, "qwen3_voice_description", "")
    q3["ref_audio_path"] = getattr(body, "qwen3_ref_audio_path", "")
    q3["ref_text"] = getattr(body, "qwen3_ref_text", "")
    q3["temperature"] = getattr(body, "qwen3_temperature", 0.9)
    q3["top_p"] = getattr(body, "qwen3_top_p", 1.0)
    q3["top_k"] = getattr(body, "qwen3_top_k", 50)
    q3["repetition_penalty"] = getattr(body, "qwen3_repetition_penalty", 1.05)
    q3["attn_implementation"] = getattr(body, "qwen3_attn_implementation", "eager")
    q3["use_torch_compile"] = getattr(body, "qwen3_use_torch_compile", False)
    q3["use_sentence_chunking"] = getattr(body, "qwen3_use_sentence_chunking", False)
    q3["sentence_max_chars"] = int(getattr(body, "qwen3_sentence_max_chars", 0) or 0)

    # Hot-reload in memory
    if "edge_tts" not in config.tts_config:
        config.tts_config["edge_tts"] = {}
    config.tts_config["edge_tts"]["voice"] = body.voice
    config.tts_config["edge_tts"]["rate"] = rate_str

    config.tts_config["gpt_sovits"] = {
        "host": body.gptsovits_host,
        "port": body.gptsovits_port,
        "dir": body.gptsovits_dir,
        "text_lang": body.gptsovits_text_lang,
        "prompt_lang": body.gptsovits_prompt_lang,
        "speed_factor": body.gptsovits_speed,
        "temperature": body.gptsovits_temperature,
        "top_p": body.gptsovits_top_p,
        "top_k": body.gptsovits_top_k,
        "repetition_penalty": body.gptsovits_repetition_penalty,
        "ref_audio_path": body.gptsovits_ref_audio_path,
        "prompt_text": body.gptsovits_prompt_text,
    }
    config.tts_config["qwen3_tts"] = dict(q3)

    # ── KokoroTTS ─────────────────────────────────────────────────────────────
    if "kokoro" not in data["tts"]:
        data["tts"]["kokoro"] = {}
    kok = data["tts"]["kokoro"]
    kok["voice"] = body.kokoro_voice
    kok["lang"] = body.kokoro_lang
    kok["speed"] = body.kokoro_speed
    kok["auto_detect_lang"] = body.kokoro_auto_detect_lang
    config.tts_config["kokoro"] = dict(kok)

    data["default_tts"] = body.engine
    _save_yaml(y, data)

    config.default_tts = body.engine

    return {"ok": True}


@router.post("/settings/tts/test")
async def test_tts_config(request: Request):
    """Synthesize a short test phrase with the current TTS config and return base64 audio."""
    import base64
    config = request.app.state.config
    tts_type = config.default_tts
    if not tts_type or tts_type == "none":
        return {"ok": False, "error": "TTS is disabled"}
    try:
        from src.tts.registry import get_tts_provider
        tts_cfg: dict = {"type": tts_type}
        if tts_type == "edge_tts":
            tts_cfg["voice"] = config.get_tts_voice()
            tts_cfg["rate"] = config.tts_config.get("edge_tts", {}).get("rate", "+0%")
        elif tts_type == "gpt_sovits":
            tts_cfg.update(config.tts_config.get("gpt_sovits", {}))
            tts_cfg["type"] = "gpt_sovits"
        elif tts_type == "kokoro":
            tts_cfg.update(config.tts_config.get("kokoro", {}))
            tts_cfg["type"] = "kokoro"
        elif tts_type == "qwen3_tts":
            tts_cfg.update(config.tts_config.get("qwen3_tts", {}))
            tts_cfg["type"] = "qwen3_tts"
        else:
            return {"ok": False, "error": f"Unknown TTS type: {tts_type}"}

        provider = get_tts_provider(tts_cfg)

        # Pick test text that matches the voice language to avoid synthesis failure
        _lang = None
        if tts_type == "edge_tts":
            _voice = tts_cfg.get("voice", "")
            _lang = _voice.split("-")[0].lower() if _voice else "zh"
        elif tts_type == "kokoro":
            _lang = tts_cfg.get("lang", "zh")
        elif tts_type == "qwen3_tts":
            _lang = config.tts_config.get("qwen3_tts", {}).get("lang", "zh")
        if _lang and _lang.startswith("ja"):
            test_text = "こんにちは、音声テストです。"
        elif _lang and not _lang.startswith("zh"):
            test_text = "Hello, this is a voice test."
        else:
            test_text = "你好，这是一条语音测试。"

        audio_bytes = await provider.synthesize(test_text)
        if not audio_bytes:
            raise RuntimeError("empty audio returned from provider")
        audio_b64 = base64.b64encode(audio_bytes).decode()
        mime = "audio/mpeg" if tts_type == "edge_tts" else "audio/wav"
        return {"ok": True, "audio": audio_b64, "mime": mime}
    except Exception as e:
        logger.warning(f"[tts/test] {e}")
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Profiles
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/profiles")
async def list_profiles(request: Request):
    from src.config.profile_loader import ProfileLoader
    loader = ProfileLoader()
    profiles = loader.list_profiles()
    sm = request.app.state.session_manager
    ordered_ids = sm._profile_order  # full order including unloaded profiles
    id_to_idx = {pid: i for i, pid in enumerate(ordered_ids)}
    profiles.sort(key=lambda p: (id_to_idx.get(p["profile_id"], 999), p.get("display_name", "")))
    return {"profiles": profiles}


def _normalize_profile_ids(raw: Any) -> List[str]:
    """从请求体中解析出非空字符串 id 列表，避免 null/类型不一致导致 422。"""
    if not raw or not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if x is not None and str(x).strip()]


@router.put("/profiles/order")
async def set_profile_order(request: Request):
    """Set the display order of profiles (settings list and main UI session list).
    必须定义在 PUT /profiles/{{profile_id}} 之前，否则 'order' 会被当作 profile_id 匹配到更新人格接口导致 422。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw = body.get("profile_ids") if isinstance(body, dict) else None
    ids = _normalize_profile_ids(raw)
    sm = request.app.state.session_manager
    sm.set_profile_order(ids)
    return {"ok": True}


class ProfileUpdateBody(BaseModel):
    display_name: str
    base_prompt: str = ""
    style_constraint: str = ""
    chaos_enabled: bool = False
    avatar: str = ""
    # Per-profile GPT-SoVITS overrides (None = don't modify; "" = clear/use global default)
    gpt_sovits_ref_audio_path: Optional[str] = None
    gpt_sovits_ref_text: Optional[str] = None
    # Per-profile Qwen3-TTS overrides
    qwen3_tts_ref_audio_path: Optional[str] = None
    qwen3_tts_ref_text: Optional[str] = None
    qwen3_tts_instruct: Optional[str] = None
    qwen3_tts_voice_description: Optional[str] = None
    qwen3_tts_speaker: Optional[str] = None
    # Per-profile KokoroTTS overrides
    kokoro_voice: Optional[str] = None
    kokoro_lang: Optional[str] = None   # empty string = inherit global
    # Prompt overrides (None = don't touch; {} = clear override; non-empty = set)
    emotion_prompts: Optional[dict] = None         # → emotion_config.emotion_prompts
    emotion_zh_descriptions: Optional[dict] = None # → emotion_config.emotion_zh_descriptions
    energy_prompts: Optional[dict] = None          # → emotion_config.energy_prompts
    affinity_prompts: Optional[dict] = None        # → emotion_config.affinity_prompts
    # World book: file ref in lorebooks/ (None = don't touch; "" = clear ref)
    lorebook_ref: Optional[str] = None
    # Legacy inline (None = don't touch; [] = clear)
    lorebook: Optional[List[Any]] = None
    lorebook_settings: Optional[Dict[str, Any]] = None  # e.g. {"scan_turns": 10}
    # Per-profile protagonist (user) override
    user_persona: Optional[Dict[str, Any]] = None
    # De-assistant mode: prepend self-identity declaration to persona prompt
    anti_assistant_mode: Optional[bool] = None


@router.post("/profiles/create")
async def create_profile(request: Request, body: ProfileUpdateBody):
    import re
    import uuid
    slug = re.sub(r"[^a-z0-9_]", "_", body.display_name.lower())[:32].strip("_")
    # 若 slug 不含任何字母或数字（如纯中文名），退回到 UUID
    profile_id = slug if re.search(r"[a-z0-9]", slug) else f"profile_{uuid.uuid4().hex[:8]}"
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if os.path.exists(path):
        profile_id = f"{profile_id}_{uuid.uuid4().hex[:6]}"
        path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")

    card = {
        "profile_id": profile_id,
        "display_name": body.display_name,
        "base_prompt": body.base_prompt,
        "style_constraint": body.style_constraint,
        "chaos_config": {"enabled": body.chaos_enabled, "threshold_high": 0.82, "threshold_low": 0.9},
        "emotion_config": {},
        "memory_config": {"enabled": False},
        "load_into_chat": True,
    }
    os.makedirs(_PROFILES_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)

    # Register as session (1:1 binding)
    sm = request.app.state.session_manager
    session = sm.add_session(profile_id, body.display_name)
    _ensure_bg_loops(request.app, session)

    return {"ok": True, "profile_id": profile_id}


@router.post("/profiles/{profile_id}/load")
async def load_existing_profile(profile_id: str, request: Request):
    """Hot-register an existing profile JSON as a live session without restarting.
    Used by the test driver to register dynamically created test profiles.
    """
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found on disk")
    sm = request.app.state.session_manager
    if profile_id in sm._sessions:
        return {"ok": True, "profile_id": profile_id, "already_loaded": True}
    with open(path, encoding="utf-8") as f:
        card = json.load(f)
    display_name = card.get("display_name", profile_id)
    session = sm.add_session(profile_id, display_name)
    _ensure_bg_loops(request.app, session)
    return {"ok": True, "profile_id": profile_id}


@router.put("/profiles/{profile_id}")
async def update_profile(profile_id: str, body: ProfileUpdateBody):
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")

    with open(path, "r", encoding="utf-8") as f:
        card = json.load(f)

    card["display_name"] = body.display_name
    card["base_prompt"] = body.base_prompt
    card["style_constraint"] = body.style_constraint
    if "chaos_config" not in card:
        card["chaos_config"] = {}
    card["chaos_config"]["enabled"] = body.chaos_enabled
    if body.avatar is not None:
        card["avatar"] = body.avatar
    # Per-profile GPT-SoVITS overrides: None = don't touch; "" = clear; non-empty = set
    if body.gpt_sovits_ref_audio_path is not None:
        if body.gpt_sovits_ref_audio_path:
            card["gpt_sovits_ref_audio_path"] = body.gpt_sovits_ref_audio_path
        else:
            card.pop("gpt_sovits_ref_audio_path", None)
    if body.gpt_sovits_ref_text is not None:
        if body.gpt_sovits_ref_text:
            card["gpt_sovits_ref_text"] = body.gpt_sovits_ref_text
        else:
            card.pop("gpt_sovits_ref_text", None)

    for key, attr in [
        ("qwen3_tts_ref_audio_path", "qwen3_tts_ref_audio_path"),
        ("qwen3_tts_ref_text", "qwen3_tts_ref_text"),
        ("qwen3_tts_instruct", "qwen3_tts_instruct"),
        ("qwen3_tts_voice_description", "qwen3_tts_voice_description"),
        ("qwen3_tts_speaker", "qwen3_tts_speaker"),
        ("kokoro_voice", "kokoro_voice"),
        ("kokoro_lang", "kokoro_lang"),
    ]:
        val = getattr(body, attr, None)
        if val is not None:
            if val:
                card[key] = val
            else:
                card.pop(key, None)

    # Prompt content overrides — all live inside emotion_config
    for attr, ec_key in [
        ("emotion_prompts",         "emotion_prompts"),
        ("emotion_zh_descriptions", "emotion_zh_descriptions"),
        ("energy_prompts",          "energy_prompts"),
        ("affinity_prompts",        "affinity_prompts"),
    ]:
        val = getattr(body, attr, None)
        if val is not None:
            card.setdefault("emotion_config", {})
            if val:
                card["emotion_config"][ec_key] = val
            else:
                card["emotion_config"].pop(ec_key, None)
    # Migrate legacy top-level affinity_prompts into emotion_config
    if "affinity_prompts" in card and body.affinity_prompts is not None:
        card.pop("affinity_prompts", None)

    if body.lorebook_ref is not None:
        ref = (body.lorebook_ref or "").strip()
        if ref:
            card["lorebook_ref"] = ref
        else:
            card.pop("lorebook_ref", None)

    if body.lorebook_settings is not None and isinstance(body.lorebook_settings, dict):
        card.setdefault("lorebook_settings", {})
        if "scan_turns" in body.lorebook_settings:
            try:
                n = int(body.lorebook_settings["scan_turns"])
                card["lorebook_settings"]["scan_turns"] = max(1, min(50, n))
            except (TypeError, ValueError):
                pass
        if "default_entry_mode" in body.lorebook_settings:
            de = body.lorebook_settings.get("default_entry_mode")
            if de in ("keyword", "constant"):
                card["lorebook_settings"]["default_entry_mode"] = de
    if card.get("lorebook_settings") and isinstance(card["lorebook_settings"], dict):
        st = card["lorebook_settings"]
        if st.get("default_entry_mode") not in ("keyword", "constant"):
            st["default_entry_mode"] = "keyword"

    if body.lorebook is not None:
        if body.lorebook:
            lb_defaults = card.get("lorebook_settings") if isinstance(card.get("lorebook_settings"), dict) else {}
            cleaned = []
            for item in body.lorebook:
                ne = normalize_lorebook_entry_for_storage(item, lb_defaults)
                if ne:
                    cleaned.append(ne)
            if cleaned:
                card["lorebook"] = cleaned
            else:
                card.pop("lorebook", None)
        else:
            card.pop("lorebook", None)

    if body.user_persona is not None:
        up = {k: (v or "").strip() for k, v in body.user_persona.items()
              if k in ("name", "description", "personality", "role_in_story")}
        if any(up.values()):
            card["user_persona"] = up
        else:
            card.pop("user_persona", None)

    if body.anti_assistant_mode is not None:
        if body.anti_assistant_mode:
            card["anti_assistant_mode"] = True
        else:
            card.pop("anti_assistant_mode", None)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    return {"ok": True}


class LoadIntoChatBody(BaseModel):
    load_into_chat: bool


@router.put("/profiles/{profile_id}/load_into_chat")
async def set_profile_load_into_chat(profile_id: str, body: LoadIntoChatBody, request: Request):
    """Set whether this profile is loaded in the sidebar (勾选「加载到聊天」). Takes effect immediately."""
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    with open(path, "r", encoding="utf-8") as f:
        card = json.load(f)
    card["load_into_chat"] = body.load_into_chat
    with open(path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)

    sm = request.app.state.session_manager
    if body.load_into_chat:
        session = sm.load_session(profile_id)
        if session:
            _ensure_bg_loops(request.app, session)
    else:
        sm.unload_session(profile_id)

    return {"ok": True}


_PROJECT_ROOT = _KOKORO_PROJECT_ROOT   # 复用已有常量，避免重复定义

# ── Voice Design 参数提示词（拼接到声音描述后送给模型）────────────────────────
_SPEED_HINTS: dict = {"slow": "用较慢的语速说。", "fast": "用较快的语速说。", "normal": ""}
_PITCH_HINTS: dict = {"high": "音调偏高。", "low": "音调偏低。", "normal": ""}
_EMOTION_HINTS: dict = {
    "strong": "情绪表现强烈、饱满。",
    "medium": "情绪表达适中。",
    "subtle": "情绪含蓄、若有若无。",
    "normal": "",
}


class GenerateVoiceBody(BaseModel):
    voice_description: str = ""
    text: str = "你好，我是你的专属助手，很高兴认识你。"
    language: str = "Auto"
    speed: str = "normal"
    pitch: str = "normal"
    emotion_strength: str = "normal"


class SaveGeneratedVoiceBody(BaseModel):
    targets: List[str] = ["gpt_sovits", "qwen3_tts"]
    filename: str = ""
    # Optional reference text used to generate this voice; when provided,
    # it will be saved into the per-profile ref_text fields together with audio.
    ref_text: Optional[str] = None


def _generate_voice_design_sync(
    voice_description: str,
    text: str,
    language: str,
    speed: str,
    pitch: str,
    emotion_strength: str,
    model_id: str,
    device: str,
    dtype_name: str,
    attn: str,
) -> bytes:
    """独立调用 VoiceDesign 合成，不触碰主 TTS 单例，避免驱逐已预热的 CustomVoice 模型。"""
    instruct = (voice_description or "").strip() or "声音自然清晰，语气平和。"
    for hint in [_SPEED_HINTS.get(speed, ""), _PITCH_HINTS.get(pitch, ""),
                 _EMOTION_HINTS.get(emotion_strength, "")]:
        if hint:
            instruct = f"{instruct} {hint}".strip()

    try:
        import io
        import torch
        import soundfile as sf
        from qwen_tts import Qwen3TTSModel
        from src.tts.qwen3_tts_provider import _resolve_model_path, _safe_device

        device = _safe_device(device)
        dtype = torch.bfloat16
        if dtype_name and "float32" in dtype_name.lower():
            dtype = torch.float32
        elif dtype_name and "float16" in dtype_name.lower():
            dtype = torch.float16
        if device == "cpu" and dtype == torch.bfloat16:
            dtype = torch.float32

        attn_impl = "flash_attention_2" if attn == "flash_attention_2" else "eager"
        load_path = _resolve_model_path(model_id)
        device_map_arg: Any = {"": device} if device != "cpu" else "cpu"

        model = Qwen3TTSModel.from_pretrained(
            load_path,
            device_map=device_map_arg,
            dtype=dtype,
            attn_implementation=attn_impl,
        )
        wavs, sr = model.generate_voice_design(
            text=(text or "").strip() or "你好，我是你的专属助手，很高兴认识你。",
            language=language or "Auto",
            instruct=instruct,
        )
        buf = io.BytesIO()
        sf.write(buf, wavs[0], sr, format="WAV")
        return buf.getvalue()
    except ImportError as e:
        logger.warning("[generate-voice] 缺少依赖: %s", e)
        return b""
    except Exception as e:
        logger.exception("[generate-voice] 合成失败: %s", e)
        raise


@router.post("/profiles/{profile_id}/generate-voice")
async def generate_profile_voice(profile_id: str, body: GenerateVoiceBody, request: Request):
    """用 Qwen3-TTS VoiceDesign 按描述生成参考音色 WAV，自动保存预览并返回音频字节。"""
    from fastapi.responses import Response as _Resp
    cfg = request.app.state.config
    qcfg = cfg.tts_config.get("qwen3_tts") or {}

    local_vd = os.path.join(_PROJECT_ROOT, "models", "Qwen-Qwen3-TTS-12Hz-1.7B-VoiceDesign")
    model_id = local_vd if os.path.isdir(local_vd) else "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
    device = qcfg.get("device", "cuda:0")
    dtype  = qcfg.get("dtype", "bfloat16")
    attn   = qcfg.get("attn_implementation", "eager")

    try:
        wav_bytes = await asyncio.to_thread(
            _generate_voice_design_sync,
            body.voice_description, body.text, body.language,
            body.speed, body.pitch, body.emotion_strength,
            model_id, device, dtype, attn,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not wav_bytes:
        raise HTTPException(status_code=500, detail="合成失败，请确认 Qwen3-TTS VoiceDesign 模型已安装")

    voices_dir = os.path.join(_PROJECT_ROOT, "Voices")
    os.makedirs(voices_dir, exist_ok=True)
    preview_path = os.path.join(voices_dir, f"{profile_id}_preview.wav")
    with open(preview_path, "wb") as f:
        f.write(wav_bytes)

    return _Resp(content=wav_bytes, media_type="audio/wav")


@router.post("/profiles/{profile_id}/save-generated-voice")
async def save_generated_voice(profile_id: str, body: SaveGeneratedVoiceBody):
    """将预览 WAV 保存为永久参考音频，并更新 profile 的 ref_audio_path/ref_text 字段。"""
    preview_path = os.path.join(_PROJECT_ROOT, "Voices", f"{profile_id}_preview.wav")
    if not os.path.isfile(preview_path):
        raise HTTPException(status_code=404, detail="未找到预览文件，请先生成音色")

    final_name = (body.filename.strip() or f"{profile_id}_voice.wav")
    if not final_name.endswith(".wav"):
        final_name += ".wav"
    final_path = os.path.join(_PROJECT_ROOT, "Voices", final_name)
    shutil.copy2(preview_path, final_path)
    rel_path = os.path.join("Voices", final_name)

    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    with open(path, "r", encoding="utf-8") as f:
        card = json.load(f)
    # Persist audio path for selected targets
    if "gpt_sovits" in body.targets:
        card["gpt_sovits_ref_audio_path"] = rel_path
    if "qwen3_tts" in body.targets:
        card["qwen3_tts_ref_audio_path"] = rel_path

    # Optionally persist the reference text alongside the audio so that
    # subsequent sessions and UIs can display/override it.
    ref_text = (body.ref_text or "").strip() if body.ref_text is not None else None
    if ref_text:
        if "gpt_sovits" in body.targets:
            card["gpt_sovits_ref_text"] = ref_text
        if "qwen3_tts" in body.targets:
            card["qwen3_tts_ref_text"] = ref_text
    with open(path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)

    return {"ok": True, "saved_path": rel_path}


@router.post("/profiles/{profile_id}/avatar")
async def upload_avatar(profile_id: str, file: UploadFile = File(...)):
    """Save an uploaded image as this profile's avatar."""
    profile_path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(profile_path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")

    # Determine extension from upload filename (default to .png)
    ext = os.path.splitext(file.filename or "")[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        raise HTTPException(status_code=400, detail="Unsupported image format")

    os.makedirs(_STATIC_AVATARS_DIR, exist_ok=True)
    dest_filename = f"{profile_id}{ext}"
    dest_path = os.path.join(_STATIC_AVATARS_DIR, dest_filename)

    contents = await file.read()
    with open(dest_path, "wb") as f:
        f.write(contents)

    avatar_url = f"avatars/{dest_filename}"

    # Update the profile JSON
    with open(profile_path, "r", encoding="utf-8") as f:
        card = json.load(f)
    card["avatar"] = avatar_url
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)

    return {"ok": True, "avatar": avatar_url}


@router.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: str, request: Request):
    sm = request.app.state.session_manager

    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")

    # If profile is active, switch away first (so periodic engines won't keep references)
    # If deleting the active profile, switch to another one first
    if sm._current_id == profile_id:
        other = next((sid for sid in sm._sessions if sid != profile_id), None)
        if other:
            sm.switch(other)
        else:
            sm._current_id = None
            sm._save_meta()

    # Best-effort: stop background loops and close cached DB handles first
    ase_engine = getattr(request.app.state, "ase_engine", None)
    if ase_engine and hasattr(ase_engine, "stop_session"):
        try:
            await ase_engine.stop_session(profile_id)
        except Exception:
            pass

    session_obj = None
    try:
        session_obj = sm._sessions.get(profile_id)
    except Exception:
        session_obj = None
    if session_obj and hasattr(session_obj, "shutdown"):
        try:
            session_obj.shutdown()
        except Exception:
            pass

    # Deregister session so cached stores can be garbage-collected.
    sm.remove_session(profile_id)

    # Best-effort shutdown memory manager to release ChromaDB file handles.
    managers = getattr(request.app.state, "memory_managers", None)
    mgr = managers.pop(profile_id, None) if isinstance(managers, dict) else None
    if mgr and hasattr(mgr, "shutdown"):
        try:
            mgr.shutdown()
        except Exception:
            pass

    # Remove profile json
    os.remove(path)

    # Remove profile data directory (chat history etc.) with retries.
    data_dir = os.path.join(_PROFILES_DIR, profile_id)
    if os.path.isdir(data_dir):
        import time
        last_err = None
        remaining: list[str] = []
        for _ in range(10):
            try:
                # Give OS/GC a moment to release file locks.
                try:
                    import gc
                    gc.collect()
                except Exception:
                    pass
                shutil.rmtree(data_dir)
                last_err = None
                break
            except Exception as e:
                last_err = e
                # Windows may take a moment to release file locks.
                try:
                    remaining = sorted(os.listdir(data_dir))
                except Exception:
                    remaining = []
                time.sleep(0.5)
        if last_err is not None:
            logger.error(
                "[delete_profile] cleanup_failed profile=%s dir=%s err=%s remaining=%s",
                profile_id, data_dir, last_err, remaining[:20],
            )
            # 后台继续重试，给 ChromaDB GC 更多时间释放句柄
            import asyncio as _asyncio
            async def _deferred_rmtree(path: str) -> None:
                for delay in (3, 8, 20):
                    await _asyncio.sleep(delay)
                    try:
                        shutil.rmtree(path)
                        logger.info("[delete_profile] deferred cleanup ok: %s", path)
                        return
                    except Exception:
                        pass
                logger.warning("[delete_profile] deferred cleanup gave up: %s", path)
            _asyncio.create_task(_deferred_rmtree(data_dir))
            return {"ok": True, "cleanup_failed": True, "remaining": remaining}

    return {"ok": True}


# ── Profile Wizard (LLM-based profile generation) ────────────────────────────

class ProfileWizardBody(BaseModel):
    description: str
    preset_name: str = ""
    disable_emotion:    bool = False
    disable_affinity:   bool = False
    disable_reflection: bool = False
    disable_memory:     bool = False


@router.post("/profiles/wizard")
async def wizard_generate_profile(body: ProfileWizardBody, request: Request):
    """Use the active LLM to generate a persona profile from a text description."""
    import re
    from src.llm.registry import get_provider, get_provider_for_role
    from src.config.prompt_loader import get_locale

    config = request.app.state.config
    if body.preset_name:
        preset = config.get_llm_preset(body.preset_name)
    else:
        preset = config.get_analysis_preset() or config.get_active_llm_preset()

    if not preset:
        raise HTTPException(status_code=503, detail="No LLM preset configured")

    provider = get_provider(preset)
    public_mode = True  # [wizard]
    locale = get_locale()
    emotion_keys = emotion_keys_csv_for_wizard(public_mode=public_mode)

    _extra_zh = ""
    _extra_en = ""
    if locale == "zh":
        system_prompt = (
            f"你是一个 AI 伴侣应用的角色设计助手。\n"
            f"根据用户的描述，生成完整的人格档案，格式为 JSON 对象，包含以下所有字段。\n"
            f"只输出原始 JSON——不加 markdown 代码块，不加注释，不加额外字段。\n\n"
            f"字段说明：\n"
            f"  display_name（字符串）：简短的角色名\n"
            f"  base_prompt（字符串）：800-2000 字。丰富的身份描述，涵盖：核心身份/背景，表面行为（3-5 个特征），深层性格（亲密后才展现），"
            f"{_extra_zh}核心人格特质，表达规则（禁止说/做什么），记忆与互动风格\n"
            f"  style_constraint（字符串）：200-400 字。语气指南、字数限制（如'40-150字'）、禁止用语、对用户的称谓、句式风格规则\n"
            f"  core_anchor（字符串）：3～6 条分号分隔的特质陈述（每条不超过 20 字）。"
            f"这些是角色**无论经历什么都不会改变**的本质特征，以简短口语化的行为倾向描述（如「遇强则强，绝不低头」），而非单个形容词。"
            f"缺点和矛盾性特质同样有资格。禁止包含格式规范或回复长度要求。\n"
        )
    else:
        system_prompt = (
            f"You are a creative character design assistant for an AI companion app.\n"
            f"Based on the user's description, generate a COMPLETE persona profile as a JSON object with EXACTLY these fields.\n"
            f"Output ONLY the raw JSON object — no markdown fences, no explanation, no extra keys.\n\n"
            f"Fields:\n"
            f"  display_name (string): short character name\n"
            f"  base_prompt (string): 800-2000 chars. Rich identity block covering: core identity/background, "
            f"surface demeanor (3-5 behavioral traits), deep nature (what emerges with intimacy), "
            f"{_extra_en}core personality traits, expression rules (what NOT to say/do), "
            f"memory & interaction style\n"
            f"  style_constraint (string): 200-400 chars. Tone guide, length limit (e.g. '40-150字'), "
            f"forbidden patterns, address term for user, sentence style rules\n"
            f"  core_anchor (string): 3-6 semicolon-separated trait statements (max ~20 chars each). "
            f"These are the character's IMMUTABLE traits that never change regardless of experience. "
            f"Use concrete behavioral statements (e.g. 'stands firm against pressure, never backs down'), not single adjectives. "
            f"Flaws and contradictions are welcome. Do NOT include formatting rules or reply-length constraints.\n"
        )
    # emotion / energy section (already locale-aware)
    if not body.disable_emotion:
        if locale == "zh":
            system_prompt += (
                f"  emotion_prompts（对象）：键为情绪名，值为 3-4 条【行为指令】数组，"
                "告诉 AI 在该情绪下如何说话——涵盖：语气/语域、句子长短、用词倾向、节奏、多说/少说什么。"
                "禁止写场景描述或第三人称旁白。每条必须是可执行规则，例如：'语气变短促，多用反问句，减少解释性语句'。"
                f"必须包含以下所有键：{emotion_keys}\n"
                "  emotion_zh_descriptions（对象）：与 emotion_prompts 相同的键，"
                "值为 4-8 个汉字的角色视角情绪短语\n"
                "  energy_prompts（对象）：键固定为 '0','10','30','60','80'，每个值为 60-150 字的【行为指令】，"
                "规定该能量等级下的回复风格——回复长短、主动性（是否主动开启话题）、措辞的活力感、节奏。"
                "从枯竭(0)到满血(80)递进。禁止写场景描述。"
                "示例 '0'：'回复极短（1-2句），只被动回应，语速感迟缓，多用省略号。'\n"
            )
        else:
            system_prompt += (
                f"  emotion_prompts (object): keys are emotion names, values are arrays of 3-4 BEHAVIORAL DIRECTIVES "
                "telling the AI HOW to write/speak in this emotional state — covering: tone/register, sentence length, vocabulary, "
                "pacing, what to say more/less of. NO scene descriptions or third-person observations. "
                "Each bullet is an actionable rule, e.g. 'keep replies clipped, use rhetorical questions, drop explanations'. "
                f"Use ALL of these keys: {emotion_keys}\n"
                "  emotion_zh_descriptions (object): same keys as emotion_prompts, "
                "values are 4-8 Chinese characters describing the emotion in the character's voice\n"
                "  energy_prompts (object): keys '0','10','30','60','80', values are strings 60-150 chars each. "
                "Each value is a DIRECTIVE for reply style at that energy level — specify: reply length, "
                "initiative (start topics or only respond?), vocabulary energy, pacing. "
                "Scale from exhausted(0) to peak(80). NO scene descriptions. "
                "Example '0': 'keep replies to 1-2 sentences, only respond when spoken to, use ellipses to convey sluggishness.'\n"
            )

    if not body.disable_affinity:
        if locale == "zh":
            system_prompt += (
                "  affinity_prompts（对象）：键为 '-100','0','200','400','600','800','1000','1200'，"
                "值为 80-160 字的【行为指令】，精确规定角色在该关系阶段的行为方式。"
                "每条使用祈使句，明确说明：情感距离、是否允许身体接触/亲密、使用的词汇/语气、该阶段禁止的行为。"
                "示例（'0' 陌生人）：'保持礼貌的社交距离。回应简短客气，不主动透露私人信息。禁止身体接触（蹭、依偎）和亲密称呼。观察对方但不表现出明显依恋。' "
                "从敌意/冷漠（-100）→ 克制/有礼（0）→ 友好（200-400）→ 温暖/信任（600-800）→ 深度亲密（1000-1200）。"
                "这些指令被 AI 作为强制约束读取，因此要具体、规定性，而非描述性。\n"
            )
        else:
            system_prompt += (
                "  affinity_prompts (object): keys '-100','0','200','400','600','800','1000','1200', "
                "values are BEHAVIORAL DIRECTIVES (80-160 chars each) that PRESCRIBE exactly how the character must act at each relationship tier. "
                "Each entry should be written in imperative style, explicitly stating: what emotional distance to keep, whether physical contact/intimacy is allowed, "
                "what vocabulary/tone to use, and what behaviors are FORBIDDEN at this stage. "
                "Example for '0' (stranger): '保持礼貌的社交距离。回应简短客气，不主动透露私人信息。禁止身体接触（蹭、依偎）和亲密称呼。观察对方但不表现出明显依恋。' "
                "Progress from hostile/cold (-100) → reserved/polite (0) → friendly (200-400) → warm/trusting (600-800) → deeply intimate (1000-1200). "
                "These are read by the AI as hard behavioral constraints, so be specific and prescriptive, not descriptive.\n"
            )

    if not body.disable_reflection:
        if locale == "zh":
            system_prompt += (
                "  reflection_custom_prompt（字符串）：200-350 字。必须以约 150 字的人格摘要开头，"
                "格式为 '【角色要点】你是{名字}: {1-2句关键特质}\\n\\n'，然后是以 AI 角色自身口吻写的第一人称内省指令——"
                "例如 '此刻你独自思考，你在想什么...'——非第三人称旁白。"
                "角色以自己的声音反思内心状态。重要：thought 字段输出必须 ≤30 个汉字——保持指令简洁。\n"
            )
        else:
            system_prompt += (
                "  reflection_custom_prompt (string): 200-350 chars. MUST start with a ~150-char persona summary block "
                "in the format '【角色要点】你是{name}: {key traits in 1-2 sentences}\\n\\n', then first-person introspection "
                "instructions written as the AI character itself — e.g. '此刻你独自思考，你在想什么...' — NOT third-person observer. "
                "The character reflects on its own inner state in its own voice. "
                "IMPORTANT: the 'thought' field output must be ≤30 Chinese chars (≤20 English words) — keep instructions concise so the model stays brief.\n"
            )

    if not body.disable_memory:
        if locale == "zh":
            system_prompt += (
                "  memory_extraction_prompt（字符串）：200-350 字。必须以相同的 ~150 字人格摘要开头 "
                "'【角色要点】你是{名字}: {关键特质}\\n\\n'，然后是以 AI 自身口吻提取记忆的第一人称指令——"
                "例如 '你是{名字}的潜意识，从对话中提取关于主人的重要事实，以第一人称记录如「我记得...」'——非旁观者视角。\n"
                "  memory_day_summary_prompt（字符串）：150-250 字。必须以 '【角色要点】你是{名字}: {关键特质}\\n\\n' 开头，"
                "然后是第一人称日记指令——例如 '你是{名字}。以第一人称（「我」）写下今天的记忆碎片，口吻{角色语气}，像日记而非报告'\n"
            )
        else:
            system_prompt += (
                "  memory_extraction_prompt (string): 200-350 chars. MUST start with the same ~150-char persona summary block "
                "'【角色要点】你是{name}: {key traits}\\n\\n', then first-person extraction instructions where the AI extracts "
                "memories as itself — e.g. '你是{name}的潜意识，从对话中提取关于主人的重要事实，以第一人称记录如「我记得...」' — NOT an outside observer.\n"
                "  memory_day_summary_prompt (string): 150-250 chars. MUST start with '【角色要点】你是{name}: {key traits}\\n\\n', "
                "then first-person diary instruction — e.g. '你是{name}。以第一人称（「我」）写下今天的记忆碎片，口吻{character tone}，像日记而非报告'\n"
            )

    user_msg = f"请为以下描述生成人格：{body.description}" if locale == "zh" else f"Create a persona for: {body.description}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    from src.utils.debug_logger import log_secondary_llm_call, log_secondary_llm_response
    preset_name = preset.get("name", preset.get("model", "unknown"))
    model_name = preset.get("model", "unknown")
    log_secondary_llm_call(role="wizard", messages=messages, model=model_name, gen_kwargs={})

    import time
    t0 = time.perf_counter()
    try:
        response = ""
        async for token in provider.stream_chat(messages):
            response += token

        duration_ms = int((time.perf_counter() - t0) * 1000)
        log_secondary_llm_response(role="wizard", response=response, model=model_name,
                                   duration_ms=duration_ms)

        raw = response.strip()
        # Strip optional ```json ... ``` fences
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        if m:
            raw = m.group(1).strip()

        profile_data = json.loads(raw)
        return {"ok": True, "profile": profile_data, "preset_used": preset_name}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"LLM output was not valid JSON: {e}", "raw": response[:500]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Prompt autofill (enrich incomplete persona prompts, same channel as wizard) ─

_SCOPE_TO_TOP_KEYS = {
    "emotion": ("emotion_prompts",),
    "emotion_zh": ("emotion_zh_descriptions",),
    "energy": ("energy_prompts",),
    "affinity": ("affinity_prompts",),
    "reflection": ("reflection_custom_prompt",),
    "memory": ("memory_extraction_prompt", "memory_day_summary_prompt"),
    "style": ("style_constraint",),
    "anchor": ("core_anchor",),
}


def _autofill_allowed_top_keys(scopes: Optional[List[str]]) -> set:
    if not scopes:
        return {k for keys in _SCOPE_TO_TOP_KEYS.values() for k in keys}
    out = set()
    for s in scopes:
        for k in _SCOPE_TO_TOP_KEYS.get(str(s).strip().lower(), ()):
            out.add(k)
    return out


def _is_empty_emotion_snapshot(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, list):
        text = " ".join(str(x) for x in val).strip()
        return len(text) < 12
    text = str(val).strip()
    if len(text) < 12:
        return True
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return len(lines) == 0


def _is_empty_short_string(val: Any, min_len: int = 18) -> bool:
    return not (val and len(str(val).strip()) >= min_len)


def _coerce_emotion_prompts_dict(obj: Any) -> Dict[str, List[str]]:
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for k, v in obj.items():
        key = str(k).strip()
        if not key:
            continue
        if isinstance(v, list):
            lines = [str(x).strip() for x in v if str(x).strip()]
        else:
            s = str(v or "").strip()
            lines = [ln.strip() for ln in s.splitlines() if ln.strip()] if s else []
        if lines:
            out[key] = lines
    return out


def _coerce_string_map(obj: Any) -> Dict[str, str]:
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in obj.items():
        key = str(k).strip()
        if not key:
            continue
        s = str(v or "").strip()
        if s:
            out[key] = s
    return out


def _filter_autofill_patch(
    patch: Dict[str, Any],
    snapshot: Dict[str, Any],
    fill_empty_only: bool,
    allowed_keys: set,
) -> Tuple[Dict[str, Any], List[str]]:
    """Drop patch keys disallowed or non-empty snapshot fields when fill_empty_only."""
    snap_ep = snapshot.get("emotion_prompts") if isinstance(snapshot.get("emotion_prompts"), dict) else {}
    snap_zh = snapshot.get("emotion_zh_descriptions") if isinstance(snapshot.get("emotion_zh_descriptions"), dict) else {}
    snap_en = snapshot.get("energy_prompts") if isinstance(snapshot.get("energy_prompts"), dict) else {}
    snap_af = snapshot.get("affinity_prompts") if isinstance(snapshot.get("affinity_prompts"), dict) else {}

    out: Dict[str, Any] = {}
    filled: List[str] = []

    if "emotion_prompts" in allowed_keys and "emotion_prompts" in patch:
        ep = _coerce_emotion_prompts_dict(patch.get("emotion_prompts"))
        merged: Dict[str, List[str]] = {}
        for k, lines in ep.items():
            if fill_empty_only and not _is_empty_emotion_snapshot(snap_ep.get(k)):
                continue
            merged[k] = lines
        if merged:
            out["emotion_prompts"] = merged
            filled.append("emotion_prompts")

    if "emotion_zh_descriptions" in allowed_keys and "emotion_zh_descriptions" in patch:
        zh = _coerce_string_map(patch.get("emotion_zh_descriptions"))
        merged_zh: Dict[str, str] = {}
        for k, s in zh.items():
            if fill_empty_only and not _is_empty_short_string(snap_zh.get(k), 2):
                continue
            merged_zh[k] = s
        if merged_zh:
            out["emotion_zh_descriptions"] = merged_zh
            filled.append("emotion_zh_descriptions")

    if "energy_prompts" in allowed_keys and "energy_prompts" in patch:
        en = _coerce_string_map(patch.get("energy_prompts"))
        merged_en: Dict[str, str] = {}
        for k, s in en.items():
            if fill_empty_only and not _is_empty_short_string(snap_en.get(k), 12):
                continue
            merged_en[k] = s
        if merged_en:
            out["energy_prompts"] = merged_en
            filled.append("energy_prompts")

    if "affinity_prompts" in allowed_keys and "affinity_prompts" in patch:
        af = _coerce_string_map(patch.get("affinity_prompts"))
        merged_af: Dict[str, str] = {}
        for k, s in af.items():
            if fill_empty_only and not _is_empty_short_string(snap_af.get(k), 12):
                continue
            merged_af[k] = s
        if merged_af:
            out["affinity_prompts"] = merged_af
            filled.append("affinity_prompts")

    if "reflection_custom_prompt" in allowed_keys and patch.get("reflection_custom_prompt") is not None:
        s = str(patch.get("reflection_custom_prompt") or "").strip()
        if s:
            if not fill_empty_only or _is_empty_short_string(snapshot.get("reflection_custom_prompt"), 25):
                out["reflection_custom_prompt"] = s
                filled.append("reflection_custom_prompt")

    if "memory_extraction_prompt" in allowed_keys and patch.get("memory_extraction_prompt") is not None:
        s = str(patch.get("memory_extraction_prompt") or "").strip()
        if s:
            if not fill_empty_only or _is_empty_short_string(snapshot.get("memory_extraction_prompt"), 25):
                out["memory_extraction_prompt"] = s
                filled.append("memory_extraction_prompt")

    if "memory_day_summary_prompt" in allowed_keys and patch.get("memory_day_summary_prompt") is not None:
        s = str(patch.get("memory_day_summary_prompt") or "").strip()
        if s:
            if not fill_empty_only or _is_empty_short_string(snapshot.get("memory_day_summary_prompt"), 20):
                out["memory_day_summary_prompt"] = s
                filled.append("memory_day_summary_prompt")

    if "style_constraint" in allowed_keys and patch.get("style_constraint") is not None:
        s = str(patch.get("style_constraint") or "").strip()
        if s:
            if not fill_empty_only or _is_empty_short_string(snapshot.get("style_constraint"), 20):
                out["style_constraint"] = s
                filled.append("style_constraint")

    if "core_anchor" in allowed_keys and patch.get("core_anchor") is not None:
        s = str(patch.get("core_anchor") or "").strip()
        if s:
            if not fill_empty_only or _is_empty_short_string(snapshot.get("core_anchor"), 10):
                out["core_anchor"] = s
                filled.append("core_anchor")

    return out, filled


class PromptAutofillBody(BaseModel):
    """Current form snapshot from UI; LLM returns only missing prompt fields."""
    preset_name: str = ""
    fill_empty_only: bool = True
    scopes: List[str] = Field(default_factory=list)
    snapshot: Dict[str, Any]


@router.post("/profiles/prompt_autofill")
async def prompt_autofill_profile(body: PromptAutofillBody, request: Request):
    """Fill empty emotion / energy / affinity / reflection / memory / style prompts from base_prompt."""
    import re
    from src.llm.registry import get_provider

    snap = body.snapshot or {}
    base = (snap.get("base_prompt") or "").strip()
    if len(base) < 15:
        raise HTTPException(status_code=400, detail="base_prompt too short (need at least ~15 chars for context)")

    config = request.app.state.config
    if body.preset_name:
        preset = config.get_llm_preset(body.preset_name)
    else:
        preset = config.get_analysis_preset() or config.get_active_llm_preset()

    if not preset:
        raise HTTPException(status_code=503, detail="No LLM preset configured")

    provider = get_provider(preset)
    public_mode = True  # [autofill]
    from src.config.prompt_loader import get_locale
    locale = get_locale()
    emotion_keys = emotion_keys_csv_for_wizard(public_mode=public_mode)

    allowed = _autofill_allowed_top_keys(body.scopes or None)
    if not allowed:
        raise HTTPException(status_code=400, detail="no valid scopes; use emotion, emotion_zh, energy, affinity, reflection, memory, style or leave empty for all")

    mode_note = (
        "The user chose FILL EMPTY FIELDS ONLY: output new content only for keys that are empty or clearly placeholder-short in the snapshot. "
        "If a field already has substantial user text, omit that key entirely from your output."
        if body.fill_empty_only
        else "The user allows OVERWRITE: you may replace weak or short fields with stronger content aligned to base_prompt."
    )

    keys_hint = ", ".join(sorted(allowed))

    system_prompt = (
        "You are a character prompt assistant for an AI companion app.\n"
        "You receive a JSON snapshot of an EXISTING persona (base_prompt, style_constraint, emotion/energy/affinity maps, etc.).\n"
        f"{mode_note}\n"
        "Infer personality from base_prompt and generate ONLY the prompt fields that still need content.\n"
        "Output a single JSON object with ONLY the keys you are providing. Allowed top-level keys in this request: "
        f"{keys_hint}.\n"
        "Nested objects:\n"
        + (
        f"  emotion_prompts：键必须来自 [{emotion_keys}]，值为 3-4 条【行为指令】数组，"
        "告诉 AI 在该情绪下如何说话——涵盖：语气/语域、句子长短、用词倾向、节奏、多说/少说什么。"
        "禁止写场景描述或第三人称旁白。每条必须是可执行规则，例如：'语气变短促，多用反问句，减少解释性语句'。\n"
        "  emotion_zh_descriptions：与 emotion_prompts 相同的键，值为 4-8 个汉字的角色视角情绪短语。\n"
        "  energy_prompts：键固定为 '0','10','30','60','80'，每个值为 60-150 字的【行为指令】，"
        "规定该能量等级下的回复风格：回复长短、主动性（是否主动开启话题）、措辞活力感、节奏。"
        "从枯竭(0)→满血(80)递进。禁止写场景描述。示例 '0'：'回复极短（1-2句），只被动回应，多用省略号。'\n"
        if locale == "zh" else
        f"  emotion_prompts: keys must be from [{emotion_keys}]. Values = JSON arrays of 3-4 BEHAVIORAL DIRECTIVES "
        "telling the AI HOW to speak/write in this state (tone, sentence length, vocabulary, pacing, what to emphasize or avoid). "
        "NO scene descriptions or third-person observations. Each bullet is an actionable rule, "
        "e.g. 'keep replies clipped, use rhetorical questions, drop explanations'.\n"
        "  emotion_zh_descriptions: same keys as emotion_prompts; values = 4-8 Chinese characters, in-character.\n"
        "  energy_prompts: keys exactly '0','10','30','60','80'; values = strings 60-150 chars. "
        "DIRECTIVES for reply style at that energy: length, initiative (start topics or only respond?), vocabulary energy, pacing. "
        "Scale exhausted(0)→peak(80). NO scene descriptions. Example '0': 'keep replies to 1-2 sentences, only respond when spoken to, use ellipses to convey sluggishness.'\n"
        )
        + "  affinity_prompts: keys exactly '-100','0','200','400','600','800','1000','1200'; "
        "values are BEHAVIORAL DIRECTIVES (80-160 chars each) written in imperative style — explicitly state what emotional distance to keep, "
        "whether physical contact/intimacy is allowed, what tone to use, and what behaviors are FORBIDDEN at this stage. "
        "Example for '0' (stranger): '保持礼貌的社交距离。回应简短客气，不主动透露私人信息。禁止身体接触（蹭、依偎）和亲密称呼。' "
        "Be prescriptive, not descriptive. These are hard behavioral constraints read directly by the AI.\n"
        "String keys:\n"
        "  reflection_custom_prompt: 200-400 chars. Start with ~150 chars persona summary '【角色要点】你是{name}: ...' then first-person introspection instructions for the AI. Note: the 'thought' output must be ≤30 Chinese chars (≤20 English words) — keep instructions concise.\n"
        "  memory_extraction_prompt: 200-400 chars. Same 【角色要点】 prefix, then first-person memory extraction instructions.\n"
        "  memory_day_summary_prompt: 150-280 chars. Same 【角色要点】 prefix, then first-person diary-style day summary instruction.\n"
        "  style_constraint: 150-400 chars. Tone, length limit, forbidden patterns, address term — only if missing in snapshot.\n"
        "  core_anchor: 3-6 semicolon-separated trait statements (max ~20 chars each). These are the character's IMMUTABLE traits — never-changing regardless of experience. Use concrete behavioral statements, not single adjectives. Include flaws/contradictions. Do NOT include formatting rules or reply-length constraints.\n"
        "Rules: Output ONLY raw JSON — no markdown fences, no commentary. Use the same language as base_prompt when possible (Chinese if base_prompt is Chinese).\n"
        "If nothing needs filling, output exactly: {}\n"
    )

    try:
        snap_json = json.dumps(snap, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="snapshot is not JSON-serializable")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Snapshot:\n{snap_json}"},
    ]

    from src.utils.debug_logger import log_secondary_llm_call, log_secondary_llm_response
    preset_name = preset.get("name", preset.get("model", "unknown"))
    model_name = preset.get("model", "unknown")
    log_secondary_llm_call(role="prompt_autofill", messages=messages, model=model_name, gen_kwargs={})

    import time
    t0 = time.perf_counter()
    response = ""
    try:
        async for token in provider.stream_chat(messages):
            response += token

        duration_ms = int((time.perf_counter() - t0) * 1000)
        log_secondary_llm_response(
            role="prompt_autofill", response=response, model=model_name, duration_ms=duration_ms
        )

        raw = response.strip()
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        if m:
            raw = m.group(1).strip()

        patch_raw = json.loads(raw)
        if not isinstance(patch_raw, dict):
            return {"ok": False, "error": "LLM output was not a JSON object", "raw": response[:1200]}

        patch, filled_keys = _filter_autofill_patch(patch_raw, snap, body.fill_empty_only, allowed)
        return {
            "ok": True,
            "patch": patch,
            "filled_keys": filled_keys,
            "preset_used": preset_name,
        }
    except json.JSONDecodeError as e:
        log_secondary_llm_response(
            role="prompt_autofill", response=response, model=model_name, duration_ms=int((time.perf_counter() - t0) * 1000)
        )
        return {"ok": False, "error": f"LLM output was not valid JSON: {e}", "raw": response[:1200]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Segment config ────────────────────────────────────────────────────────────

@router.get("/profiles/{profile_id}/segments")
async def get_segment_config(profile_id: str):
    """Return the merged segment config: registered segments + per-profile overrides + custom segments."""
    # Import pipeline + reflection to ensure all @register decorators have run
    from src.prompt.pipeline import build_messages  # noqa: F401 — triggers chat segment imports
    import src.core.reflection  # noqa: F401 — triggers reflection segment imports
    from src.prompt.registry import get_registered
    from src.prompt.segment_config import effective_segment_enabled, load_segment_config

    seg_cfg = load_segment_config(profile_id)
    result = []
    for seg_cls in get_registered():
        meta = seg_cfg.get_meta(seg_cls.segment_id)
        content_val = ""
        default_content_val = ""
        if hasattr(seg_cls, 'DEFAULT_CONTENT'):
            default_content_val = seg_cls.DEFAULT_CONTENT
            content_val = (meta.content or "").strip() if meta else ""
            if not content_val:
                content_val = default_content_val
        result.append({
            "segment_id":     seg_cls.segment_id,
            "label":          seg_cls.label,
            "description":    seg_cls.description,
            "is_core":        seg_cls.is_core,
            "inject_into":    seg_cls.inject_into,
            "is_readonly":    seg_cls.is_readonly,
            "content":        content_val,
            "default_content": default_content_val,
            # effective values (meta overrides class defaults)
            "priority":          meta.priority          if meta and meta.priority          is not None else seg_cls.priority,
            "enabled":           effective_segment_enabled(meta, seg_cls),
            "default_enabled":   getattr(seg_cls, "default_enabled", True),
            "trigger_mode":      meta.trigger_mode      if meta and meta.trigger_mode      is not None else seg_cls.default_trigger_mode,
            "trigger_param":     meta.trigger_param     if meta and meta.trigger_param     is not None else seg_cls.default_trigger_param,
            "trigger_keywords":  meta.trigger_keywords  if meta else None,
        })
    return {
        "segments":        result,
        "custom_segments": [c.to_dict() for c in seg_cfg.custom_segments],
    }


class SegmentMetaBody(BaseModel):
    segment_id:       str
    enabled:          bool = True
    priority:         Optional[int]   = None
    trigger_mode:     Optional[str]   = None
    trigger_param:    Optional[float] = None
    content:          Optional[str]   = None   # for ASE segments with user-editable content
    trigger_keywords: Optional[str]   = None   # comma-separated keywords for "keyword" trigger mode


class CustomSegmentBody(BaseModel):
    segment_id:       str
    label:            str
    content:          str
    priority:         int   = 50
    enabled:          bool  = True
    trigger_mode:     str   = "always"
    trigger_param:    float = 1.0
    inject_into:      str   = "chat"
    trigger_keywords: Optional[str] = None     # comma-separated keywords for "keyword" trigger mode


class SaveSegmentConfigBody(BaseModel):
    segments:        List[SegmentMetaBody]   = []
    custom_segments: List[CustomSegmentBody] = []


@router.put("/profiles/{profile_id}/segments")
async def save_segment_config_endpoint(profile_id: str, body: SaveSegmentConfigBody):
    """Persist segment configuration overrides + custom segments for a profile."""
    from src.prompt.segment_config import (
        CustomSegmentDef,
        SegmentConfig,
        SegmentMeta,
        save_segment_config,
    )

    cfg = SegmentConfig()
    for s in body.segments:
        cfg.set_meta(SegmentMeta(
            segment_id=s.segment_id,
            enabled=s.enabled,
            priority=s.priority,
            trigger_mode=s.trigger_mode,
            trigger_param=s.trigger_param,
            content=s.content,
            trigger_keywords=s.trigger_keywords,
        ))
    cfg.custom_segments = [
        CustomSegmentDef(
            segment_id=c.segment_id,
            label=c.label,
            content=c.content,
            priority=c.priority,
            enabled=c.enabled,
            trigger_mode=c.trigger_mode,
            trigger_param=c.trigger_param,
            inject_into=c.inject_into,
            trigger_keywords=c.trigger_keywords,
        )
        for c in body.custom_segments
    ]
    save_segment_config(profile_id, cfg)
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# System config
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Engine & Secondary Model Config (P4)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/settings/engines")
async def get_engines_config(request: Request):
    """Return current engine + analysis model config."""
    config = request.app.state.config
    engines = config.engines
    sec = config.secondary_models.get("analysis", {})
    return {
        "emotion_freq":          engines.get("emotion",  {}).get("classification_frequency", 5),
        "affinity_freq":         engines.get("affinity", {}).get("llm_adjust_frequency", 5),
        "affinity_delta_clamp":  engines.get("affinity", {}).get("delta_clamp", 15.0),
        "energy_interval":       engines.get("energy",   {}).get("refresh_interval", 300),
        "analysis_enabled":           sec.get("enabled", False),
        "analysis_preset":            sec.get("preset", ""),
        "analysis_temperature":       sec.get("temperature"),
        "analysis_top_p":             sec.get("top_p"),
        "analysis_presence_penalty":  sec.get("presence_penalty"),
        "analysis_frequency_penalty": sec.get("frequency_penalty"),
        "analysis_max_tokens":        sec.get("max_tokens"),
    }


class EnginesConfigBody(BaseModel):
    emotion_freq:         int   = 5
    affinity_freq:        int   = 5
    affinity_delta_clamp: float = 15.0
    energy_interval:      int   = 300


@router.post("/settings/engines")
async def save_engines_config(request: Request, body: EnginesConfigBody):
    """Persist engine params to app.yaml and hot-reload."""
    config = request.app.state.config

    y, data = _load_yaml()
    if "engines" not in data:
        data["engines"] = {}

    em = data["engines"].setdefault("emotion",  {})
    af = data["engines"].setdefault("affinity", {})
    en = data["engines"].setdefault("energy",   {})

    em["classification_frequency"] = body.emotion_freq
    af["llm_adjust_frequency"]     = body.affinity_freq
    af["delta_clamp"]              = body.affinity_delta_clamp
    en["refresh_interval"]         = body.energy_interval

    _save_yaml(y, data)

    # Hot-reload
    config.engines = {
        "emotion":  dict(em),
        "affinity": dict(af),
        "energy":   dict(en),
    }
    return {"ok": True}


@router.get("/settings/user_persona")
async def get_user_persona(request: Request):
    """Return global user persona config."""
    config = request.app.state.config
    p = config.user_persona or {}
    return {
        "name":        p.get("name", ""),
        "description": p.get("description", ""),
        "personality": p.get("personality", ""),
    }


class UserPersonaBody(BaseModel):
    name:        str = ""
    description: str = ""
    personality: str = ""


@router.post("/settings/user_persona")
async def save_user_persona(request: Request, body: UserPersonaBody):
    """Persist global user persona to app.yaml and hot-reload."""
    config = request.app.state.config
    y, data = _load_yaml()
    data["user_persona"] = {
        "name":        body.name.strip(),
        "description": body.description.strip(),
        "personality": body.personality.strip(),
    }
    _save_yaml(y, data)
    config.user_persona = dict(data["user_persona"])
    return {"ok": True}


class SecondaryModelBody(BaseModel):
    analysis_enabled:           bool           = False
    analysis_preset:            str            = ""
    analysis_temperature:       Optional[float] = None
    analysis_top_p:             Optional[float] = None
    analysis_presence_penalty:  Optional[float] = None
    analysis_frequency_penalty: Optional[float] = None
    analysis_max_tokens:        Optional[int]   = None


@router.post("/settings/secondary_models")
async def save_secondary_models(request: Request, body: SecondaryModelBody):
    """Persist secondary_models.analysis and re-register provider if enabled."""
    config = request.app.state.config

    y, data = _load_yaml()
    if "secondary_models" not in data:
        data["secondary_models"] = {}
    sec = data["secondary_models"].setdefault("analysis", {})
    sec["enabled"] = body.analysis_enabled
    sec["preset"]  = body.analysis_preset
    # Gen params: store directly in sec (ensure_analysis_provider reads from same level)
    for _k, _v in (
        ("temperature",       body.analysis_temperature),
        ("top_p",             body.analysis_top_p),
        ("presence_penalty",  body.analysis_presence_penalty),
        ("frequency_penalty", body.analysis_frequency_penalty),
        ("max_tokens",        body.analysis_max_tokens),
    ):
        if _v is not None:
            sec[_k] = _v
        else:
            sec.pop(_k, None)
    _save_yaml(y, data)

    # Hot-reload
    if "secondary_models" not in config.__dict__:
        config.secondary_models = {}
    config.secondary_models["analysis"] = dict(sec)

    # Re-register or unregister analysis provider immediately
    from src.llm.registry import register_role_provider, unregister_role_provider, get_provider
    if body.analysis_enabled and body.analysis_preset:
        preset_dict = config.llm_presets.get(body.analysis_preset)
        if preset_dict:
            overrides = {k: sec.get(k) for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "max_tokens")}
            register_role_provider("analysis", get_provider(preset_dict, overrides))
            logger.info("[settings] analysis provider re-registered → %s", body.analysis_preset)
        else:
            logger.warning("[settings] analysis preset '%s' not found in llm_presets", body.analysis_preset)
    else:
        unregister_role_provider("analysis")
        logger.info("[settings] analysis provider unregistered (disabled)")

    return {"ok": True}


@router.get("/settings/system")
async def get_system_config(request: Request):
    config = request.app.state.config
    return {
        "log_level":      config.log_level,
        "server_address": f"{config.host}:{config.port}",
    }


class SystemSaveBody(BaseModel):
    log_level: str = "info"


@router.post("/settings/system")
async def save_system_config(request: Request, body: SystemSaveBody):
    config = request.app.state.config

    valid_levels = {"debug", "info", "warning", "error"}
    if body.log_level not in valid_levels:
        raise HTTPException(status_code=400, detail=f"Invalid log_level: {body.log_level}")

    # Hot-reload
    config.log_level = body.log_level
    logging.getLogger().setLevel(getattr(logging, body.log_level.upper(), logging.INFO))

    # Persist
    y, data = _load_yaml()
    data["log_level"] = body.log_level
    _save_yaml(y, data)

    return {"ok": True}


def _read_env_proxy():
    """从 .env 读取 HTTP_PROXY / HTTPS_PROXY，供前端展示。"""
    from dotenv import dotenv_values
    env = dotenv_values(_ENV_FILE) or {}
    http_proxy = (env.get("HTTP_PROXY") or "").strip()
    https_proxy = (env.get("HTTPS_PROXY") or "").strip()
    proxy = http_proxy or https_proxy
    if proxy and proxy.startswith("http://"):
        proxy_url = proxy.replace("http://", "", 1)
    elif proxy and proxy.startswith("https://"):
        proxy_url = proxy.replace("https://", "", 1)
    else:
        proxy_url = proxy
    return {
        "proxy_enabled": bool(proxy_url),
        "proxy_url": proxy_url,
    }


@router.get("/settings/system/env")
async def get_system_env():
    """返回 .env 中的代理配置，供设置页「系统」展示。"""
    try:
        return _read_env_proxy()
    except Exception:
        return {"proxy_enabled": False, "proxy_url": ""}


class SystemEnvSaveBody(BaseModel):
    proxy_enabled: bool = False
    proxy_url: str = ""


@router.post("/settings/system/env")
async def save_system_env(body: SystemEnvSaveBody):
    """将代理配置写入 .env；重启后生效。"""
    from dotenv import set_key, unset_key
    proxy_url = (body.proxy_url or "").strip()
    if body.proxy_enabled and proxy_url:
        if "://" not in proxy_url:
            proxy_url = "http://" + proxy_url
        set_key(_ENV_FILE, "HTTP_PROXY", proxy_url, quote_mode="never")
        set_key(_ENV_FILE, "HTTPS_PROXY", proxy_url, quote_mode="never")
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
    else:
        unset_key(_ENV_FILE, "HTTP_PROXY")
        unset_key(_ENV_FILE, "HTTPS_PROXY")
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
    return {"ok": True}


@router.get("/profiles/{profile_id}/engine_config")
async def get_profile_engine_config(profile_id: str, request: Request):
    """返回该人格的引擎覆盖项；无覆盖时返回空对象，前端可显示为「继承全局」。
    同时附带 global_defaults 字段供前端显示「全局默认：xxx」。
    """
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    with open(path, "r", encoding="utf-8") as f:
        card = json.load(f)
    overrides = card.get("engine_overrides") or {}
    # 展平为前端字段
    em = overrides.get("emotion", {})
    af = overrides.get("affinity", {})
    en = overrides.get("energy", {})
    rf = overrides.get("reflection", {})
    ase = overrides.get("ase", {})

    # 全局默认值（来自 app.yaml），供前端「继承」模式显示实际生效值
    cfg = getattr(getattr(request.app, "state", None), "config", None)
    global_defaults = None
    if cfg:
        g_em  = cfg.engines.get("emotion",  {})
        g_af  = cfg.engines.get("affinity", {})
        g_en  = cfg.engines.get("energy",   {})
        g_rf  = cfg.reflection  # Dict[str, Any]
        g_ase = cfg.ase         # Dict[str, Any]
        global_defaults = {
            "emotion_enabled":      g_em.get("enabled", True),
            "emotion_freq":         g_em.get("classification_frequency", 5),
            "affinity_enabled":     g_af.get("enabled", True),
            "affinity_freq":        g_af.get("llm_adjust_frequency", 5),
            "affinity_delta_clamp": g_af.get("delta_clamp", 15),
            "energy_enabled":       g_en.get("enabled", True),
            "energy_interval":      g_en.get("refresh_interval", 300),
            "reflection_enabled":   g_rf.get("enabled", True),
            "ase_enabled":          g_ase.get("enabled", True),
        }

    return {
        "emotion_enabled":       em.get("enabled"),          # None = 继承全局
        "emotion_freq":          em.get("classification_frequency"),
        "affinity_enabled":      af.get("enabled"),
        "affinity_freq":         af.get("llm_adjust_frequency"),
        "affinity_delta_clamp":  af.get("delta_clamp"),
        "energy_enabled":        en.get("enabled"),
        "energy_interval":       en.get("refresh_interval"),
        "reflection_enabled":    rf.get("enabled"),
        "ase_enabled":           ase.get("enabled"),
        "global_defaults":       global_defaults,
    }


class ProfileEngineConfigBody(BaseModel):
    emotion_enabled:      Optional[bool]  = None   # None = 继承全局
    emotion_freq:         Optional[int]   = None
    affinity_enabled:     Optional[bool]  = None
    affinity_freq:        Optional[int]   = None
    affinity_delta_clamp: Optional[float] = None
    energy_enabled:       Optional[bool]  = None
    energy_interval:      Optional[int]   = None
    reflection_enabled:   Optional[bool]  = None
    ase_enabled:          Optional[bool]  = None


@router.put("/profiles/{profile_id}/engine_config")
async def save_profile_engine_config(profile_id: str, body: ProfileEngineConfigBody):
    """更新该人格的引擎覆盖项（仅写入非 None 字段；enabled=None 表示删除覆盖、恢复全局继承）。"""
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    with open(path, "r", encoding="utf-8") as f:
        card = json.load(f)
    if "engine_overrides" not in card:
        card["engine_overrides"] = {}
    ov = card["engine_overrides"]

    # Helper: set or remove a key inside an engine sub-dict
    def _set(engine: str, key: str, val):
        if val is None:
            # remove override → inherit global
            if engine in ov and key in ov[engine]:
                del ov[engine][key]
                if not ov[engine]:
                    del ov[engine]
        else:
            ov.setdefault(engine, {})[key] = val

    _set("emotion",     "enabled",                  body.emotion_enabled)
    _set("affinity",    "enabled",                  body.affinity_enabled)
    _set("energy",      "enabled",                  body.energy_enabled)
    _set("reflection",  "enabled",                  body.reflection_enabled)
    _set("ase",         "enabled",                  body.ase_enabled)
    if body.emotion_freq is not None:
        ov.setdefault("emotion", {})["classification_frequency"] = max(1, min(100, body.emotion_freq))
    if body.affinity_freq is not None:
        ov.setdefault("affinity", {})["llm_adjust_frequency"] = max(1, min(100, body.affinity_freq))
    if body.affinity_delta_clamp is not None:
        ov.setdefault("affinity", {})["delta_clamp"] = max(0.5, min(100, float(body.affinity_delta_clamp)))
    if body.energy_interval is not None:
        ov.setdefault("energy", {})["refresh_interval"] = max(60, min(3600, body.energy_interval))

    with open(path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    return {"ok": True}


@router.get("/profiles/{profile_id}/reflection_config")
async def get_profile_reflection_config(profile_id: str):
    """返回该人格的自省配置：custom_prompt + 所有 reflection/ASE 段落（来自统一 segments_config）。"""
    from src.prompt.pipeline import build_messages  # noqa: F401 — triggers @register for all segments
    from src.prompt.registry import get_registered
    from src.prompt.base import targets_reflection, targets_ase
    from src.prompt.segment_config import effective_segment_enabled, load_segment_config

    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    with open(path, "r", encoding="utf-8") as f:
        card = json.load(f)
    ref = card.get("reflection_config") or {}
    custom_prompt = (ref.get("custom_prompt") or "").strip()
    if not custom_prompt:
        custom_prompt = (card.get("base_prompt") or "").strip()
        source = "base_prompt"
    else:
        source = "reflection_config"
    chat_inject_topic_hint = ref.get("chat_inject_topic_hint", True)
    long_absence_hours = ref.get("long_absence_hours", 48)

    seg_cfg = load_segment_config(profile_id)
    segments = []
    for seg_cls in get_registered():
        if not (targets_reflection(seg_cls.inject_into) or targets_ase(seg_cls.inject_into)):
            continue
        meta = seg_cfg.get_meta(seg_cls.segment_id)
        default_content = getattr(seg_cls, 'DEFAULT_CONTENT', '')
        content_val = (meta.content or "").strip() if meta else ""
        if not content_val:
            content_val = default_content
        segments.append({
            "segment_id":     seg_cls.segment_id,
            "label":          seg_cls.label,
            "description":    seg_cls.description,
            "is_core":        seg_cls.is_core,
            "inject_into":    seg_cls.inject_into,
            "is_readonly":    seg_cls.is_readonly,
            "is_builtin":     True,
            "content":        content_val,
            "default_content": default_content,
            "priority":       meta.priority      if meta and meta.priority      is not None else seg_cls.priority,
            "enabled":        effective_segment_enabled(meta, seg_cls),
            "default_enabled": getattr(seg_cls, "default_enabled", True),
            "trigger_mode":   meta.trigger_mode  if meta and meta.trigger_mode  is not None else seg_cls.default_trigger_mode,
            "trigger_param":  meta.trigger_param if meta and meta.trigger_param is not None else seg_cls.default_trigger_param,
        })
    # Custom segments with inject_into targeting reflection or ase
    for c in seg_cfg.custom_segments:
        if targets_reflection(c.inject_into) or targets_ase(c.inject_into):
            segments.append({
                "segment_id":   c.segment_id,
                "label":        c.label,
                "description":  "",
                "is_core":      False,
                "inject_into":  c.inject_into,
                "is_readonly":  False,
                "is_builtin":   False,
                "content":      c.content,
                "default_content": "",
                "priority":     c.priority,
                "enabled":      c.enabled,
                "trigger_mode": c.trigger_mode,
                "trigger_param": c.trigger_param,
            })
    segments.sort(key=lambda s: (s["inject_into"], s["priority"]))
    return {
        "custom_prompt": custom_prompt,
        "source": source,
        "chat_inject_topic_hint": chat_inject_topic_hint,
        "long_absence_hours": long_absence_hours,
        "segments": segments,
    }


class ProfileReflectionConfigBody(BaseModel):
    custom_prompt: Optional[str] = None
    chat_inject_topic_hint: Optional[bool] = None
    long_absence_hours: Optional[int] = None
    segments: Optional[List[Dict[str, Any]]] = None  # unified reflection+ASE segments
    # Keep for backward compat but no longer write to profile.json:
    segment_overrides: Optional[Dict[str, Dict[str, Any]]] = None  # DEPRECATED
    custom_segments: Optional[List[Dict[str, Any]]] = None  # DEPRECATED


def _normalize_trigger_param(mode: str, param) -> float:
    """校验并归一化 trigger_param。"""
    try:
        v = float(param)
    except (TypeError, ValueError):
        v = float("nan")
    if mode == "time_window":
        # start*100+end，0<=start,end<=23；允许 start>end（跨午夜，与 PromptSegment.is_triggered 一致）
        if not (v == v):  # NaN
            return 510.0
        raw = int(round(v))
        start = raw // 100
        end = raw % 100
        if 0 <= start <= 23 and 0 <= end <= 23:
            return float(start * 100 + end)
        logger.warning(
            "[reflection_config] time_window trigger_param 非法 %r → 回退 510 (5–10 点)",
            param,
        )
        return 510.0
    if not (v == v):  # NaN（非 time_window）
        return 1.0
    if mode == "cooldown":
        return max(1, min(10080, v))
    if mode == "probability":
        return max(0, min(1, v))
    if mode == "first_after_silence":
        return max(1, min(10080, v))
    if mode == "every_n_turns":
        return max(1, min(200, v))
    return v


@router.put("/profiles/{profile_id}/reflection_config")
async def save_profile_reflection_config(profile_id: str, body: ProfileReflectionConfigBody):
    """更新该人格的自省配置。segments 写入 segments_config.json；其余写入 profile.json。"""
    from src.prompt.base import targets_reflection, targets_ase

    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    with open(path, "r", encoding="utf-8") as f:
        card = json.load(f)
    if "reflection_config" not in card:
        card["reflection_config"] = {}
    ref = card["reflection_config"]
    if body.custom_prompt is not None:
        ref["custom_prompt"] = (body.custom_prompt or "").strip()
    if body.chat_inject_topic_hint is not None:
        ref["chat_inject_topic_hint"] = bool(body.chat_inject_topic_hint)
    if body.long_absence_hours is not None:
        ref["long_absence_hours"] = max(1, min(720, int(body.long_absence_hours)))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)

    # Handle segment changes → write to segments_config.json
    if body.segments is not None:
        from src.prompt.segment_config import load_segment_config, save_segment_config, SegmentMeta, CustomSegmentDef
        from src.prompt.pipeline import build_messages  # noqa: F401 — ensure @register has run
        from src.prompt.registry import get_registered
        builtin_ids = {cls.segment_id for cls in get_registered()}
        seg_cfg = load_segment_config(profile_id)

        # Remove existing reflection/ASE custom segments (will be replaced)
        seg_cfg.custom_segments = [
            c for c in seg_cfg.custom_segments
            if not (targets_reflection(c.inject_into) or targets_ase(c.inject_into))
        ]

        for s in body.segments:
            seg_id = (s.get("segment_id") or "").strip()
            if not seg_id:
                continue
            if seg_id in builtin_ids:
                # Built-in: update override in segments_config
                seg_cfg.set_meta(SegmentMeta(
                    segment_id=seg_id,
                    enabled=s.get("enabled", True),
                    priority=s.get("priority"),
                    trigger_mode=s.get("trigger_mode") or None,
                    trigger_param=_normalize_trigger_param(s.get("trigger_mode", "always"), s.get("trigger_param", 1.0)),
                    content=(s.get("content") or "").strip() or None,
                ))
            else:
                # Custom: add to custom_segments with inject_into
                inject = (s.get("inject_into") or "reflection_ase").strip().lower()
                valid_injects = ("chat", "reflection", "ase", "reflection_ase", "chat_reflection", "chat_ase", "all")
                if inject not in valid_injects:
                    inject = "reflection_ase"
                seg_cfg.custom_segments.append(CustomSegmentDef(
                    segment_id=seg_id,
                    label=(s.get("label") or seg_id).strip(),
                    content=(s.get("content") or "").strip(),
                    inject_into=inject,
                    priority=int(s.get("priority", 50)),
                    enabled=s.get("enabled", True),
                    trigger_mode=(s.get("trigger_mode") or "always").strip(),
                    trigger_param=_normalize_trigger_param(s.get("trigger_mode", "always"), s.get("trigger_param", 1.0)),
                ))
        save_segment_config(profile_id, seg_cfg)

    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# Memory config (P6)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/settings/memory")
async def get_memory_config(request: Request):
    """返回全局记忆配置。"""
    config = request.app.state.config
    return config.get_memory_config()


class MemoryConfigBody(BaseModel):
    enabled: bool = False
    vector_enabled: bool = False
    extraction_frequency: int = 5
    extraction_weight_threshold: float = 0.5  # 自动抽取：weight 大于此值才采纳
    max_facts_in_prompt: int = 8
    max_history_turns: int = 20
    day_summary_enabled: bool = False
    day_summary_keep_days: int = 14
    day_summary_max_messages: int = 100      # 写日记单日最多消息条数
    day_summary_max_conv_chars: int = 12000  # 对话内容最大字符数
    # 检索槽位配额
    recent_fact_quota: int = 3
    semantic_fact_quota: int = 2
    recent_days: int = 3
    semantic_distance: float = 0.45
    vector_dedup_distance_threshold: float = 0.1  # 写入向量时：距离<此值视为「几乎相同」删旧写新
    daily_run_at_hour: int = 0
    daily_run_at_minute: int = 5
    daily_forgetting_enabled: bool = False
    daily_decay_factor: float = 0.998
    daily_decay_min_weight: float = 0.1
    daily_reinforcement_enabled: bool = False
    daily_consolidation_enabled: bool = False
    daily_consolidation_after_days: int = 30
    daily_consolidation_weight_below: float = 0.3
    daily_consolidation_batch_max: int = 15


@router.post("/settings/memory")
async def save_memory_config(request: Request, body: MemoryConfigBody):
    """持久化全局记忆配置到 app.yaml 并热重载。"""
    config = request.app.state.config

    y, data = _load_yaml()
    if "memory" not in data:
        data["memory"] = {}
    m = data["memory"]
    m["enabled"] = body.enabled
    m["vector_enabled"] = body.vector_enabled
    m["extraction_frequency"] = body.extraction_frequency
    m["extraction_weight_threshold"] = max(0.0, min(1.0, float(body.extraction_weight_threshold)))
    m["max_facts_in_prompt"] = body.max_facts_in_prompt
    m["max_history_turns"] = max(1, min(200, body.max_history_turns))
    m["day_summary_enabled"] = body.day_summary_enabled
    m["day_summary_keep_days"] = body.day_summary_keep_days
    m["day_summary_max_messages"] = max(10, min(500, body.day_summary_max_messages))
    m["day_summary_max_conv_chars"] = max(2000, min(100000, body.day_summary_max_conv_chars))
    # 槽位配额
    m["recent_fact_quota"] = body.recent_fact_quota
    m["semantic_fact_quota"] = body.semantic_fact_quota
    m["recent_days"] = body.recent_days
    m["semantic_distance"] = body.semantic_distance
    m["vector_dedup_distance_threshold"] = max(0.01, min(0.5, float(body.vector_dedup_distance_threshold)))
    m["daily_run_at_hour"] = max(0, min(23, body.daily_run_at_hour))
    m["daily_run_at_minute"] = max(0, min(59, body.daily_run_at_minute))
    m["daily_forgetting_enabled"] = body.daily_forgetting_enabled
    m["daily_decay_factor"] = max(0.9, min(1.0, float(body.daily_decay_factor)))
    m["daily_decay_min_weight"] = max(0.0, min(1.0, float(body.daily_decay_min_weight)))
    m["daily_reinforcement_enabled"] = body.daily_reinforcement_enabled
    m["daily_consolidation_enabled"] = body.daily_consolidation_enabled
    m["daily_consolidation_after_days"] = max(7, min(365, body.daily_consolidation_after_days))
    m["daily_consolidation_weight_below"] = max(0.0, min(1.0, float(body.daily_consolidation_weight_below)))
    m["daily_consolidation_batch_max"] = max(5, min(30, body.daily_consolidation_batch_max))
    _save_yaml(y, data)

    # Hot-reload
    config.memory = dict(m)

    # Invalidate all cached MemoryManagers so they pick up the new config on next request
    managers = getattr(request.app.state, "memory_managers", None)
    if managers:
        managers.clear()
        logger.info("[settings] 全局记忆配置已更新，MemoryManagers 全部失效")

    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# Embedding Model Settings (standalone tab)
# ─────────────────────────────────────────────────────────────────────────────

def _embedding_api_key_env_present() -> bool:
    """与 src/memory/embedding.py 中 Gemini 密钥解析顺序一致（任一存在即视为已配置）。"""
    return bool(
        os.environ.get("EMBEDDING_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
    )


@router.get("/settings/embedding")
async def get_embedding_config(request: Request):
    """返回 embedding 配置。"""
    config = request.app.state.config
    mem = config.get_memory_config()
    emb = mem.get("embedding", {})
    has_key = bool((emb.get("api_key") or "").strip()) or _embedding_api_key_env_present()
    return {
        "provider":    emb.get("provider", "gemini"),
        "model":       emb.get("model") or emb.get("gemini_model", "gemini-embedding-001"),
        "base_url":    emb.get("base_url", ""),
        "has_key":     has_key,
        "local_model": emb.get("local_model", "all-MiniLM-L6-v2"),
        "device":      emb.get("device", "auto"),
        "offline":     emb.get("offline", False),
    }


class EmbeddingConfigBody(BaseModel):
    provider:    str  = "gemini"
    model:       str  = "gemini-embedding-001"
    base_url:    str  = ""
    api_key:     str  = ""
    local_model: str  = "all-MiniLM-L6-v2"
    device:      str  = "auto"
    offline:     bool = False


@router.post("/settings/embedding")
async def save_embedding_config(request: Request, body: EmbeddingConfigBody):
    """持久化 embedding 配置到 app.yaml，API key 写 .env。"""
    y, data = _load_yaml()
    if "memory" not in data:
        data["memory"] = {}
    if "embedding" not in data["memory"]:
        data["memory"]["embedding"] = {}
    emb = data["memory"]["embedding"]
    emb["provider"]    = body.provider
    emb["model"]       = body.model
    emb["gemini_model"] = body.model   # backward compat
    emb["base_url"]    = body.base_url
    emb["api_key"]     = ""            # never store in yaml
    emb["local_model"] = body.local_model
    emb["device"]      = body.device
    emb["offline"]     = body.offline
    _save_yaml(y, data)

    # API key → .env（EMBEDDING_API_KEY；嵌入层亦读取 GOOGLE_API_KEY / GEMINI_API_KEY，见 embedding.py）
    from dotenv import set_key, unset_key
    raw_key = body.api_key if isinstance(body.api_key, str) else ""
    sk = raw_key.strip()
    if sk:
        set_key(_ENV_FILE, "EMBEDDING_API_KEY", sk)
        os.environ["EMBEDDING_API_KEY"] = sk
    elif raw_key and not sk:
        # 仅空白（如提示「填一个空格」）→ 清除本应用写入的 EMBEDDING_API_KEY
        unset_key(_ENV_FILE, "EMBEDDING_API_KEY")
        os.environ.pop("EMBEDDING_API_KEY", None)
    # raw_key == ""：留空不修改 .env 中已有密钥

    # Hot-reload
    config = request.app.state.config
    config.memory["embedding"] = dict(emb)
    # Invalidate MemoryManagers
    managers = getattr(request.app.state, "memory_managers", None)
    if managers:
        managers.clear()
    logger.info("[settings] embedding 配置已保存 provider=%s model=%s", body.provider, body.model)
    return {"ok": True}


class TestEmbeddingBody(BaseModel):
    """请求体：当前表单的 embedding 配置，用于测试不落库。"""
    provider: str = "gemini"
    model: str = "gemini-embedding-001"
    gemini_model: str = "gemini-embedding-001"  # backward compat
    base_url: str = ""
    api_key: str = ""
    local_model: str = "all-MiniLM-L6-v2"
    offline: bool = False
    device: Optional[str] = None


def _run_embedding_test(config: dict) -> tuple:
    """同步执行：创建 EmbeddingProvider 并 embed 一句测试文本。返回 (ok, latency_ms, error)。"""
    from src.memory.embedding import EmbeddingProvider
    try:
        provider = EmbeddingProvider(config)
        if not provider.is_available():
            return False, 0.0, "无可用 embedding 后端"
        t0 = time.perf_counter()
        vecs = provider.embed(["test"])
        latency_ms = (time.perf_counter() - t0) * 1000
        if vecs is None:
            return False, 0.0, "embed 返回空"
        return True, latency_ms, None
    except Exception as e:
        logger.warning("[settings] embedding 测试失败: %s", e, exc_info=True)
        return False, 0.0, str(e)


# embedding 测试最长等待时间（秒），超时后返回错误，避免永远「正在测试」
_EMBED_TEST_TIMEOUT = 30.0


@router.post("/settings/embedding/test")
@router.post("/settings/memory/test-embedding")  # backward compat
async def test_embedding(request: Request, body: TestEmbeddingBody):
    """用当前表单的 embedding 配置测试是否可用并返回耗时（不写入配置）。"""
    mem = request.app.state.config.get_memory_config()
    emb_default = mem.get("embedding", {})
    # Resolve API key: form field → env（与 embedding.py 一致）
    api_key = (body.api_key or "").strip() or (
        os.environ.get("EMBEDDING_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
    )
    config = {
        "provider":    body.provider,
        "model":       body.model or body.gemini_model,
        "gemini_model": body.model or body.gemini_model,
        "base_url":    body.base_url,
        "api_key":     api_key,
        "local_model": body.local_model,
        "offline":     body.offline,
        "device":      body.device or emb_default.get("device", "auto"),
    }
    try:
        ok, latency_ms, err = await asyncio.wait_for(
            asyncio.to_thread(_run_embedding_test, config),
            timeout=_EMBED_TEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return JSONResponse(status_code=504, content={"ok": False, "error": f"请求超时（{int(_EMBED_TEST_TIMEOUT)} 秒），请检查网络或 API"})
    if ok:
        return {"ok": True, "latency_ms": round(latency_ms, 1)}
    return JSONResponse(status_code=503, content={"ok": False, "error": err or "未知错误"})


class ProfileMemoryConfigBody(BaseModel):
    enabled: Optional[bool] = None
    vector_enabled: Optional[bool] = None
    extraction_frequency: Optional[int] = None
    extraction_weight_threshold: Optional[float] = None
    max_facts_in_prompt: Optional[int] = None
    max_history_turns: Optional[int] = None
    day_summary_enabled: Optional[bool] = None
    extraction_prompt: Optional[str] = None
    day_summary_prompt: Optional[str] = None
    extraction_llm_preset: Optional[str] = None
    embedding_provider: Optional[str] = None
    # 检索槽位配额（人格级可覆盖全局）
    recent_fact_quota: Optional[int] = None
    semantic_fact_quota: Optional[int] = None
    recent_days: Optional[int] = None
    semantic_distance: Optional[float] = None
    vector_dedup_distance_threshold: Optional[float] = None
    # 群聊近期合并进单聊 history
    group_chat_self_recap_max_utterances_per_group: Optional[int] = None
    group_chat_self_recap_max_groups: Optional[int] = None
    group_chat_merge_into_history: Optional[bool] = None
    merged_history_max_messages: Optional[int] = None
    # 每日遗忘（只跑已加载且昨日有对话）
    daily_forgetting_enabled: Optional[bool] = None
    daily_decay_factor: Optional[float] = None
    daily_decay_min_weight: Optional[float] = None
    daily_reinforcement_enabled: Optional[bool] = None
    daily_consolidation_enabled: Optional[bool] = None
    daily_consolidation_after_days: Optional[int] = None
    daily_consolidation_weight_below: Optional[float] = None
    daily_consolidation_batch_max: Optional[int] = None


# ─────────────────────────────────────────────────────────────────────────────
# Reflection / ASE / VLM config
# ─────────────────────────────────────────────────────────────────────────────

_ASE_MODE_DEFAULTS = {
    "low":    {"check_interval": 600, "min_silent_seconds": 900,  "urgency_threshold": 0.55, "min_interval": 1800, "max_per_24h": 5,  "max_consecutive_without_response": 3},
    "medium": {"check_interval": 300, "min_silent_seconds": 300,  "urgency_threshold": 0.35, "min_interval": 600,  "max_per_24h": 15, "max_consecutive_without_response": 5},
    "high":   {"check_interval": 120, "min_silent_seconds": 180,  "urgency_threshold": 0.25, "min_interval": 240,  "max_per_24h": 30, "max_consecutive_without_response": 10},
    "game":   {"check_interval": 60,  "min_silent_seconds": 90,   "urgency_threshold": 0.40, "min_interval": 120,  "max_per_24h": 40, "max_consecutive_without_response": 0},
    "focus":  {"check_interval": 600, "min_silent_seconds": 1200, "urgency_threshold": 0.70, "min_interval": 1800, "max_per_24h": 3,  "max_consecutive_without_response": 2},
}


@router.get("/settings/reflection")
async def get_reflection_config(request: Request):
    """Return current reflection, ASE and VLM settings."""
    config = request.app.state.config
    ref  = config.get_reflection_config()
    ase  = config.get_ase_config()
    vlm  = config.get_vlm_config()
    sec_ref = config.secondary_models.get("reflection", {})

    # Build per-mode params merging YAML values over defaults
    yaml_modes = ase.get("modes", {})
    ase_modes = {}
    for mode_name, defaults in _ASE_MODE_DEFAULTS.items():
        yaml_m = yaml_modes.get(mode_name, {})
        m = dict(defaults)
        for key in ("check_interval", "min_silent_seconds", "urgency_threshold",
                    "min_interval", "max_per_24h"):
            if key in yaml_m:
                m[key] = yaml_m[key]
        # max_consecutive: absent/null in YAML → 0 (unlimited) in UI
        raw_mc = yaml_m.get("max_consecutive_without_response")
        if raw_mc is None:
            m["max_consecutive_without_response"] = defaults.get("max_consecutive_without_response", 0)
        else:
            m["max_consecutive_without_response"] = int(raw_mc)
        # extra_behavior: user override (empty = use locale default from ase.yaml)
        msg_cfg = yaml_m.get("message_config") or {}
        m["extra_behavior"] = (msg_cfg.get("extra_behavior") or "").strip()
        ase_modes[mode_name] = m

    return {
        # Reflection engine
        "reflection_enabled":         ref.get("enabled", True),
        "reflection_interval":        ref.get("interval_seconds", 180),
        "reflection_interval_min":    ref.get("interval_min_seconds", 20),
        "reflection_interval_max":    ref.get("interval_max_seconds", 1800),
        "reflection_retry":           ref.get("retry_seconds", 60),
        "reflection_recent_turns":             ref.get("recent_turns", 10),
        "reflection_recent_dialogue_max_turns": ref.get("recent_dialogue_max_turns", 8),
        "reflection_ttl":                    ref.get("state_ttl_seconds", 600),
        "reflection_idle_throttle_after":    ref.get("idle_throttle_after_seconds", 900),
        "reflection_idle_interval":          ref.get("idle_interval_seconds", 600),
        "reflection_model_enabled":          sec_ref.get("enabled", True),
        "reflection_primary_preset":  sec_ref.get("primary_preset", ""),
        "reflection_fallback_preset": sec_ref.get("fallback_preset", ""),
        "reflection_temperature":         sec_ref.get("gen_params", {}).get("temperature"),
        "reflection_top_p":               sec_ref.get("gen_params", {}).get("top_p"),
        "reflection_presence_penalty":    sec_ref.get("gen_params", {}).get("presence_penalty"),
        "reflection_frequency_penalty":   sec_ref.get("gen_params", {}).get("frequency_penalty"),
        "reflection_max_tokens":          sec_ref.get("gen_params", {}).get("max_tokens"),
        # ASE
        "ase_enabled":  ase.get("enabled", True),
        "ase_mode":     ase.get("mode", "medium"),
        "ase_vlm_mode": ase.get("vlm_mode", "random"),
        "ase_modes":    ase_modes,
        # VLM
        "vlm_enabled":          vlm.get("enabled", False),
        "vlm_model_preset":     vlm.get("model_preset", ""),
        "vlm_for_chat":         vlm.get("for_chat", True),
        "vlm_for_ase":          vlm.get("for_ase", True),
        "vlm_ase_wait_seconds": vlm.get("ase_wait_seconds", 3),
    }


class ReflectionConfigBody(BaseModel):
    # Reflection engine
    reflection_enabled:         bool  = True
    reflection_interval:        int   = 180
    reflection_interval_min:    int   = 20
    reflection_interval_max:    int   = 1800
    reflection_retry:           int   = 60
    reflection_recent_turns:              int   = 10
    reflection_recent_dialogue_max_turns: int   = 8
    reflection_ttl:                       int   = 600
    reflection_idle_throttle_after: int   = 900
    reflection_idle_interval:       int   = 600
    reflection_model_enabled:       bool  = True
    reflection_primary_preset:  str   = ""
    reflection_fallback_preset: str   = ""
    reflection_temperature:         Optional[float] = None
    reflection_top_p:               Optional[float] = None
    reflection_presence_penalty:    Optional[float] = None
    reflection_frequency_penalty:   Optional[float] = None
    reflection_max_tokens:          Optional[int]   = None
    # ASE
    ase_enabled:  bool = True
    ase_mode:     str  = "medium"
    ase_vlm_mode: str  = "random"
    ase_modes:    Optional[Dict[str, Any]] = None   # per-mode param overrides
    # VLM
    vlm_enabled:          bool = False
    vlm_model_preset:     str  = ""
    vlm_for_chat:         bool = True
    vlm_for_ase:          bool = True
    vlm_ase_wait_seconds: int  = 3


@router.post("/settings/reflection")
async def save_reflection_config(request: Request, body: ReflectionConfigBody):
    """Persist reflection/ASE/VLM config to app.yaml and hot-reload."""
    config = request.app.state.config

    y, data = _load_yaml()

    # ── reflection section ─────────────────────────────────────────────────
    ref = data.setdefault("reflection", {})
    ref["enabled"]                     = body.reflection_enabled
    ref["interval_seconds"]            = body.reflection_interval
    ref["interval_min_seconds"]        = body.reflection_interval_min
    ref["interval_max_seconds"]        = body.reflection_interval_max
    ref["retry_seconds"]               = body.reflection_retry
    ref["recent_turns"]                   = body.reflection_recent_turns
    ref["recent_dialogue_max_turns"]      = body.reflection_recent_dialogue_max_turns
    ref["state_ttl_seconds"]              = body.reflection_ttl
    ref["idle_throttle_after_seconds"] = body.reflection_idle_throttle_after
    ref["idle_interval_seconds"]       = body.reflection_idle_interval

    # secondary_models.reflection
    sec = data.setdefault("secondary_models", {})
    sec_ref = sec.setdefault("reflection", {})
    sec_ref["enabled"]         = body.reflection_model_enabled
    sec_ref["primary_preset"]  = body.reflection_primary_preset
    sec_ref["fallback_preset"] = body.reflection_fallback_preset
    gp: dict = {}
    if body.reflection_temperature is not None:
        gp["temperature"] = body.reflection_temperature
    if body.reflection_top_p is not None:
        gp["top_p"] = body.reflection_top_p
    if body.reflection_presence_penalty is not None:
        gp["presence_penalty"] = body.reflection_presence_penalty
    if body.reflection_frequency_penalty is not None:
        gp["frequency_penalty"] = body.reflection_frequency_penalty
    if body.reflection_max_tokens is not None:
        gp["max_tokens"] = body.reflection_max_tokens
    sec_ref["gen_params"] = gp

    # ── ase section ────────────────────────────────────────────────────────
    ase = data.setdefault("ase", {})
    ase["enabled"]  = body.ase_enabled
    ase["mode"]     = body.ase_mode
    ase["vlm_mode"] = body.ase_vlm_mode

    # Per-mode params (only update the 6 tunable keys; preserve message_config etc.)
    if body.ase_modes:
        existing_modes = ase.setdefault("modes", {})
        _tunable = ("check_interval", "min_silent_seconds", "urgency_threshold",
                    "min_interval", "max_per_24h")
        for mode_name, mp in body.ase_modes.items():
            m = existing_modes.setdefault(mode_name, {})
            for key in _tunable:
                if key in mp:
                    m[key] = mp[key]
            # max_consecutive: 0 in UI → remove key (unlimited); >0 → set it
            mc = mp.get("max_consecutive_without_response")
            if mc is not None:
                if int(mc) == 0:
                    m.pop("max_consecutive_without_response", None)
                else:
                    m["max_consecutive_without_response"] = int(mc)
            # extra_behavior: save into message_config; empty string = use locale default
            if "extra_behavior" in mp:
                msg_cfg = m.setdefault("message_config", {})
                msg_cfg["extra_behavior"] = (mp["extra_behavior"] or "").strip()

    # ── vlm section ────────────────────────────────────────────────────────
    vlm = data.setdefault("vlm", {})
    vlm["enabled"]           = body.vlm_enabled
    vlm["model_preset"]      = body.vlm_model_preset
    vlm["for_chat"]          = body.vlm_for_chat
    vlm["for_ase"]           = body.vlm_for_ase
    vlm["ase_wait_seconds"]  = body.vlm_ase_wait_seconds

    _save_yaml(y, data)

    # ── Hot-reload ─────────────────────────────────────────────────────────
    config.reflection = dict(ref)
    config.ase        = dict(ase)
    config.vlm        = dict(vlm)
    config.secondary_models["reflection"] = dict(sec_ref)

    # ── 引擎启停：UI 开关改变时，无需重启服务器即可生效 ────────────────────
    # ReflectionEngine：task 不存在或已结束时才 start，避免重复创建
    refl_engine = getattr(request.app.state, "reflection_engine", None)
    if refl_engine is not None:
        task_alive = refl_engine._task and not refl_engine._task.done()
        if body.reflection_enabled and not task_alive:
            asyncio.create_task(refl_engine.start())
            logger.info("[settings] ReflectionEngine started via UI toggle")

    # AseEngine：start() 内部会为各 session 创建 loop，已有的不重复创建
    ase_engine = getattr(request.app.state, "ase_engine", None)
    if ase_engine is not None:
        any_task_alive = any(
            not t.done() for t in ase_engine._tasks.values()
        )
        if body.ase_enabled and not any_task_alive:
            asyncio.create_task(ase_engine.start())
            logger.info("[settings] AseEngine started via UI toggle")

    logger.info("[settings] reflection/ASE/VLM config saved")
    return {"ok": True}


@router.get("/profiles/{profile_id}/memory_config")
async def get_profile_memory_config(profile_id: str):
    """返回 profile 级别的记忆配置覆盖项。"""
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    with open(path, "r", encoding="utf-8") as f:
        card = json.load(f)
    return {"memory_config": card.get("memory_config", {})}


@router.put("/profiles/{profile_id}/memory_config")
async def save_profile_memory_config(profile_id: str, body: ProfileMemoryConfigBody,
                                     request: Request):
    """更新 profile 级别记忆配置，触发 MemoryManager 热重建。"""
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")

    with open(path, "r", encoding="utf-8") as f:
        card = json.load(f)

    if "memory_config" not in card:
        card["memory_config"] = {}
    mc = card["memory_config"]

    # 仅更新非 None 字段
    updates = body.model_dump(exclude_none=True)
    # embedding_provider 映射到 nested dict
    if "embedding_provider" in updates:
        if "embedding" not in mc:
            mc["embedding"] = {}
        mc["embedding"]["provider"] = updates.pop("embedding_provider")
    mc.update(updates)
    if mc.get("max_history_turns") is not None:
        mc["max_history_turns"] = max(1, min(200, int(mc["max_history_turns"])))
    if mc.get("extraction_weight_threshold") is not None:
        mc["extraction_weight_threshold"] = max(0.0, min(1.0, float(mc["extraction_weight_threshold"])))
    if mc.get("vector_dedup_distance_threshold") is not None:
        mc["vector_dedup_distance_threshold"] = max(0.01, min(0.5, float(mc["vector_dedup_distance_threshold"])))
    if mc.get("group_chat_self_recap_max_utterances_per_group") is not None:
        mc["group_chat_self_recap_max_utterances_per_group"] = max(1, min(100, int(mc["group_chat_self_recap_max_utterances_per_group"])))
    if mc.get("group_chat_self_recap_max_groups") is not None:
        mc["group_chat_self_recap_max_groups"] = max(1, min(20, int(mc["group_chat_self_recap_max_groups"])))
    if mc.get("merged_history_max_messages") is not None:
        mc["merged_history_max_messages"] = max(2, min(500, int(mc["merged_history_max_messages"])))

    with open(path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)

    # 热重建：销毁旧 MemoryManager，下次 /chat 会懒创建新的
    managers = getattr(request.app.state, "memory_managers", {})
    if profile_id in managers:
        del managers[profile_id]
        logger.info("[settings_ext] MemoryManager 热重建 profile=%s", profile_id)

    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# Special dates (重要日期) — per-profile
# ─────────────────────────────────────────────────────────────────────────────

class SpecialDateEntry(BaseModel):
    date: str   # "MM-DD"
    label: str  # e.g. "生日"

class SpecialDatesBody(BaseModel):
    dates: List[SpecialDateEntry]


@router.get("/profiles/{profile_id}/special_dates")
async def get_special_dates(profile_id: str):
    """返回 profile 的重要日期列表。"""
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    with open(path, "r", encoding="utf-8") as f:
        card = json.load(f)
    return {"dates": card.get("special_dates", [])}


@router.put("/profiles/{profile_id}/special_dates")
async def set_special_dates(profile_id: str, body: SpecialDatesBody):
    """覆盖保存 profile 的重要日期列表（原子写入）。"""
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")

    with open(path, "r", encoding="utf-8") as f:
        card = json.load(f)

    card["special_dates"] = [e.model_dump() for e in body.dates]

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

    logger.info("[settings_ext] special_dates saved profile=%s count=%d",
                profile_id, len(body.dates))
    return {"ok": True}


# ── Persona Evolution ─────────────────────────────────────────────────────────

@router.get("/profiles/{profile_id}/persona_evolution")
async def get_persona_evolution(profile_id: str):
    """返回当前人格演化状态：原件、演化版、core_anchor。"""
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    with open(path, "r", encoding="utf-8") as f:
        card = json.load(f)
    storage_root = os.path.join(_PROFILES_DIR, profile_id)
    from src.core.persona_evolution import get_changelog
    return {
        "base_prompt_original": card.get("base_prompt", ""),
        "style_constraint_original": card.get("style_constraint", ""),
        "persona_evolved": card.get("persona_evolved") or {},
        "changelog": get_changelog(storage_root),
    }


class PersonaEvolutionAnchorBody(BaseModel):
    preset_name: str = ""


@router.post("/profiles/{profile_id}/persona_evolution/extract_anchor")
async def extract_persona_anchor(profile_id: str, _body: PersonaEvolutionAnchorBody, request: Request):
    """（重新）提炼 core_anchor 并写入 profile.persona_evolved.core_anchor。"""
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    with open(path, "r", encoding="utf-8") as f:
        card = json.load(f)

    base = (card.get("base_prompt") or "").strip()
    style = (card.get("style_constraint") or "").strip()
    if len(base) < 15:
        raise HTTPException(status_code=400, detail="base_prompt too short")

    persona_name = card.get("display_name") or card.get("name") or profile_id
    app = request.app
    from src.core.persona_evolution import extract_anchor
    anchor = await extract_anchor(persona_name, base, style, app, profile_id=profile_id)
    if not anchor:
        raise HTTPException(status_code=500, detail="anchor extraction failed")

    card.setdefault("persona_evolved", {})["core_anchor"] = anchor
    with open(path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)

    return {"ok": True, "core_anchor": anchor}


class PersonaEvolutionSaveBody(BaseModel):
    """前端手动编辑后保存演化版本和/或 anchor。"""
    core_anchor: Optional[str] = None
    base_prompt: Optional[str] = None
    style_constraint: Optional[str] = None
    enabled: Optional[bool] = None
    min_interval_turns: Optional[int] = None


@router.put("/profiles/{profile_id}/persona_evolution")
async def save_persona_evolution(profile_id: str, body: PersonaEvolutionSaveBody):
    """保存用户手动编辑的 persona_evolved 字段（anchor / 演化版 base_prompt / style / 开关）。"""
    path = os.path.join(_PROFILES_DIR, f"{profile_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    with open(path, "r", encoding="utf-8") as f:
        card = json.load(f)

    card.setdefault("persona_evolved", {})
    if body.core_anchor is not None:
        card["persona_evolved"]["core_anchor"] = body.core_anchor.strip()
    if body.base_prompt is not None:
        card["persona_evolved"]["base_prompt"] = body.base_prompt.strip()
    if body.style_constraint is not None:
        card["persona_evolved"]["style_constraint"] = body.style_constraint.strip()
    if body.enabled is not None:
        card["persona_evolved"]["enabled"] = body.enabled
    if body.min_interval_turns is not None:
        card["persona_evolved"]["min_interval_turns"] = max(10, body.min_interval_turns)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    return {"ok": True}


class PersonaRollbackBody(BaseModel):
    version: int


@router.post("/profiles/{profile_id}/persona_evolution/rollback")
async def rollback_persona_evolution(profile_id: str, body: PersonaRollbackBody):
    """回滚 persona_evolved 到指定版本（base_prompt + style_constraint）。"""
    storage_root = os.path.join(_PROFILES_DIR, profile_id)
    from src.core.persona_evolution import rollback
    ok = rollback(profile_id, storage_root, body.version)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Version {body.version} not found in changelog")
    return {"ok": True}
