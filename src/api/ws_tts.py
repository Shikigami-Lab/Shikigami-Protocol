import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, WebSocket

from src.tts.registry import get_tts_provider
from src.tts.text_preprocessor import preprocess_for_tts
from src.tts.instruct_builder import build_instruct
from src.utils.debug_logger import log_tts_request, log_tts_response, log_error

logger = logging.getLogger(__name__)
router = APIRouter()

# 项目根目录（server.py 所在目录），用于解析参考音频等相对路径
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _resolve_ref_audio_path(path: str) -> str:
    """将相对路径解析为相对于项目根；绝对路径原样返回。"""
    if not (path and path.strip()):
        return path or ""
    path = path.strip()
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(_PROJECT_ROOT, path))


def _build_tts_config(websocket: WebSocket, profile_id_override: Optional[str] = None) -> dict:
    """Build the TTS config dict from app config + profile (session or override).

    When profile_id_override is set (e.g. group chat replay), use that profile's
    TTS overrides; otherwise use the current session's profile.
    """
    tts_cfg = {"type": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"}
    try:
        app = websocket.scope.get("app")
        if not (app and hasattr(app, "state")):
            return tts_cfg

        config = app.state.config
        tts_type = config.default_tts
        tts_cfg["type"] = tts_type

        if tts_type == "edge_tts":
            tts_cfg["voice"] = config.get_tts_voice()

        elif tts_type == "gpt_sovits":
            # Start with global YAML params
            tts_cfg.update(config.tts_config.get("gpt_sovits", {}))
            tts_cfg["type"] = "gpt_sovits"   # ensure type is not overwritten

            # Override ref_text / ref_audio_path from profile (override or current session)
            try:
                from src.config.profile_loader import ProfileLoader
                loader = ProfileLoader()
                profile_id = profile_id_override
                if not profile_id:
                    sm = app.state.session_manager
                    session = sm.get_current()
                    if session:
                        profile_id = session.profile_id
                if profile_id:
                    profile = loader.load(profile_id)
                    if profile.get("gpt_sovits_ref_text"):
                        tts_cfg["prompt_text"] = profile["gpt_sovits_ref_text"]
                    if profile.get("gpt_sovits_ref_audio_path"):
                        tts_cfg["ref_audio_path"] = _resolve_ref_audio_path(profile["gpt_sovits_ref_audio_path"])
            except Exception as e:
                logger.warning(f"[ws/tts] profile load failed: {e}")

            # Resolve and validate global/profile ref_audio_path: if file missing, drop it
            if tts_cfg.get("ref_audio_path"):
                resolved = _resolve_ref_audio_path(tts_cfg["ref_audio_path"])
                if resolved and os.path.isfile(resolved):
                    tts_cfg["ref_audio_path"] = resolved
                else:
                    logger.warning(f"[ws/tts] gpt_sovits ref_audio_path not found, disabling voice clone: {tts_cfg['ref_audio_path']}")
                    tts_cfg.pop("ref_audio_path", None)

        elif tts_type == "kokoro":
            tts_cfg.update(config.tts_config.get("kokoro", {}))
            tts_cfg["type"] = "kokoro"
            try:
                from src.config.profile_loader import ProfileLoader
                loader = ProfileLoader()
                profile_id = profile_id_override
                if not profile_id:
                    sm = app.state.session_manager
                    session = sm.get_current()
                    if session:
                        profile_id = session.profile_id
                if profile_id:
                    profile = loader.load(profile_id)
                    if profile.get("kokoro_voice"):
                        tts_cfg["voice"] = profile["kokoro_voice"]
                    if profile.get("kokoro_lang"):
                        tts_cfg["lang"] = profile["kokoro_lang"]
            except Exception as e:
                logger.warning(f"[ws/tts] kokoro profile load failed: {e}")

        elif tts_type == "qwen3_tts":
            tts_cfg.update(config.tts_config.get("qwen3_tts", {}))
            tts_cfg["type"] = "qwen3_tts"
            try:
                from src.config.profile_loader import ProfileLoader
                loader = ProfileLoader()
                profile_id = profile_id_override
                if not profile_id:
                    sm = app.state.session_manager
                    session = sm.get_current()
                    if session:
                        profile_id = session.profile_id
                if profile_id:
                    profile = loader.load(profile_id)
                    ref_audio = profile.get("qwen3_tts_ref_audio_path") or profile.get("gpt_sovits_ref_audio_path")
                    ref_text = profile.get("qwen3_tts_ref_text") or profile.get("gpt_sovits_ref_text")
                    if ref_audio:
                        tts_cfg["ref_audio_path"] = _resolve_ref_audio_path(ref_audio)
                    if ref_text:
                        tts_cfg["ref_text"] = ref_text
                    if profile.get("qwen3_tts_instruct"):
                        tts_cfg["instruct"] = profile["qwen3_tts_instruct"]
                    if profile.get("qwen3_tts_voice_description"):
                        tts_cfg["voice_description"] = profile["qwen3_tts_voice_description"]
                    if profile.get("qwen3_tts_speaker"):
                        tts_cfg["speaker"] = profile["qwen3_tts_speaker"]
            except Exception as e:
                logger.warning(f"[ws/tts] profile load failed: {e}")
            # 全局默认的 ref_audio_path 也按项目根解析
            if tts_cfg.get("ref_audio_path"):
                tts_cfg["ref_audio_path"] = _resolve_ref_audio_path(tts_cfg["ref_audio_path"])

    except Exception as e:
        logger.warning(f"[ws/tts] config build failed: {e}")

    return tts_cfg


def _get_emotion_instruct_override(websocket: WebSocket) -> str:
    """为 Qwen3-TTS 等提供情绪→instruct 扩展；预留后续 speed/pitch 等。"""
    try:
        app = websocket.scope.get("app")
        if not (app and hasattr(app, "state")):
            return ""
        emotion_engine = getattr(app.state, "emotion_engine", None)
        sm = getattr(app.state, "session_manager", None)
        if not emotion_engine or not sm:
            return ""
        session = sm.get_current()
        if not session:
            return ""
        state = emotion_engine.load_state(session)
        primary = (state.get("primary_emotion") or "").strip()
        if not primary:
            return ""
        from src.config.profile_loader import ProfileLoader
        profile = ProfileLoader().load(session.profile_id)
        emotion_zh = None
        if profile.get("emotion_config") and isinstance(profile["emotion_config"], dict):
            emotion_zh = profile["emotion_config"].get("emotion_zh_descriptions")
        return build_instruct(
            emotion=primary,
            profile_emotion_zh_descriptions=emotion_zh,
            base_instruct=None,
        )
    except Exception as e:
        logger.debug("[ws/tts] emotion instruct: %s", e)
        return ""


@router.websocket("/ws/tts")
async def tts_websocket(websocket: WebSocket):
    """
    WebSocket TTS endpoint.
    Client sends: text string
    Server sends: MP3 audio bytes
    """
    await websocket.accept()
    logger.info("[ws/tts] client connected")

    try:
        while True:
            raw = await websocket.receive_text()
            # Group chat replay sends JSON: {"text": "...", "profile_id": "..."}; single chat sends plain text
            profile_id_override = None
            if raw and raw.strip().startswith("{"):
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict) and "text" in data and "profile_id" in data:
                        raw = data.get("text") or ""
                        profile_id_override = data.get("profile_id")
                except (json.JSONDecodeError, TypeError):
                    pass
            text = preprocess_for_tts(raw or "")
            if not text:
                continue

            # Rebuild per message so profile switches take effect immediately
            tts_cfg = _build_tts_config(websocket, profile_id_override=profile_id_override)
            tts = get_tts_provider(tts_cfg)
            voice_label = tts_cfg.get("ref_audio_path", tts_cfg.get("voice", tts_cfg.get("type", "unknown")))

            # ── log: text entering TTS ───────────────────────────
            log_tts_request(text=text, voice=voice_label)

            kwargs = {}
            if tts_cfg.get("type") == "qwen3_tts":
                emotion_instruct = _get_emotion_instruct_override(websocket)
                if emotion_instruct:
                    kwargs["instruct_override"] = emotion_instruct

            try:
                audio_bytes = await tts.synthesize(text, **kwargs)
                if audio_bytes:
                    await websocket.send_bytes(audio_bytes)
                    log_tts_response(
                        text=text,
                        voice=voice_label,
                        audio_bytes=len(audio_bytes),
                        success=True,
                    )
                else:
                    log_tts_response(
                        text=text,
                        voice=voice_label,
                        audio_bytes=0,
                        success=False,
                        error="synthesize returned empty bytes",
                    )
            except Exception as e:
                logger.warning(f"[ws/tts] synthesis error: {e}")
                log_tts_response(
                    text=text,
                    voice=voice_label,
                    audio_bytes=0,
                    success=False,
                    error=str(e),
                )
                log_error("ws_tts/synthesize", str(e))

    except Exception:
        logger.info("[ws/tts] client disconnected")
