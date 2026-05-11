"""vlm.py — Vision Language Model endpoints

POST /vlm
    Describe a screenshot using a vision-capable LLM.
    Accepts base64-encoded image; returns plain-text description.
    Used in two scenarios:
      - Chat: user attaches screenshot before sending a message (source="chat")
      - ASE: heartbeat_speak requests a screenshot before proactive speech (source="ase")

POST /sessions/{session_id}/screenshot
    Frontend uploads a screenshot for ASE to read.
    Stores base64 in session.last_screenshot_b64 (in-memory, no persistence).
"""
import logging
import time
import traceback
from string import Template

from src.config.prompt_loader import get_prompt

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.utils.debug_logger import (
    log_error,
    log_vlm_error,
    log_vlm_request,
    log_vlm_result,
    log_vlm_screenshot_upload,
    log_secondary_llm_call,
    log_secondary_llm_response,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class VlmRequest(BaseModel):
    session_id: str = ""
    image_b64: str          # base64-encoded image (no data: URI prefix needed)
    source: str = "chat"    # "chat" | "ase"
    image_type: str = "screenshot"  # "screenshot" | "upload" — only for source="chat"; screenshot=屏幕截图, upload=用户上传的任意图片


class ScreenshotUploadRequest(BaseModel):
    image_b64: str
    triggered_by: str = "user"   # "user" | "ase"


@router.post("/vlm")
async def describe_image(body: VlmRequest, request: Request):
    """Describe a screenshot image using the configured vision LLM.

    Returns {"description": "..."} or {"description": "", "error": "..."}.
    If vlm.enabled=false, returns {"description": ""} immediately.
    """
    config = request.app.state.config
    vlm_cfg = config.get_vlm_config()

    if not vlm_cfg.get("enabled"):
        logger.debug("[VLM] vlm.enabled=false, returning empty description")
        return {"description": ""}

    preset_name = vlm_cfg.get("model_preset", "Gemini-3.0")
    # 特殊值："__none__"=不使用 VLM；"__active__"=使用激活聊天模型
    if preset_name == "__none__":
        logger.debug("[VLM] model_preset='__none__', returning empty description")
        return {"description": ""}
    if preset_name == "__active__":
        preset = config.get_active_llm_preset()
    else:
        preset = config.get_llm_preset(preset_name)
    if not preset or not preset.get("api_key"):
        return JSONResponse(
            status_code=503,
            content={"description": "", "error": "VLM preset not configured or missing api_key"},
        )

    model = preset.get("model", "gemini-2.5-flash")
    # Rough byte-size estimate (base64 overhead ~33%)
    image_bytes = len(body.image_b64) * 3 // 4

    log_vlm_request(
        session_id=body.session_id,
        source=body.source,
        image_bytes=image_bytes,
        model=model,
    )

    image_type = body.image_type if body.source == "chat" else "screenshot"
    t0 = time.time()
    try:
        description = await _call_vlm(preset, body.image_b64, session_id=body.session_id, image_type=image_type)
        duration_ms = int((time.time() - t0) * 1000)
        log_vlm_result(
            session_id=body.session_id,
            source=body.source,
            description_len=len(description),
            description_preview=description,
            duration_ms=duration_ms,
        )
        logger.debug("[VLM] description ready (source=%s, %d chars, %dms)",
                     body.source, len(description), duration_ms)
        return {"description": description}
    except Exception as e:
        logger.error("[VLM] describe_image error: %s", e)
        log_vlm_error(session_id=body.session_id, source=body.source, error=str(e))
        log_error("vlm", str(e), {"session_id": body.session_id, "source": body.source}, traceback_str=traceback.format_exc())
        return JSONResponse(
            status_code=503,
            content={"description": "", "error": str(e)},
        )


@router.post("/sessions/{session_id}/screenshot")
async def upload_screenshot(session_id: str, body: ScreenshotUploadRequest,
                            request: Request):
    """Frontend uploads a base64 screenshot for ASE to read.

    The screenshot is stored in session.last_screenshot_b64 (in-memory).
    ASE will read it after waiting ase_wait_seconds.
    """
    sm = request.app.state.session_manager
    session = sm.get_by_id(session_id)
    if session is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "session not found"})

    session.last_screenshot_b64 = body.image_b64
    image_bytes = len(body.image_b64) * 3 // 4

    log_vlm_screenshot_upload(
        session_id=session_id,
        image_bytes=image_bytes,
        triggered_by=body.triggered_by,
    )
    logger.debug("[VLM] screenshot uploaded session=%s triggered_by=%s size~%d bytes",
                 session_id, body.triggered_by, image_bytes)
    return {"ok": True}


def _screen_prompt(ocr_prefix: str) -> str:
    """Prompt for screen screenshot: 应用/窗口、画面内容、用户活动、屏幕文字谨慎识别。"""
    return ocr_prefix + get_prompt("vlm.screenshot")


def _upload_image_prompt() -> str:
    """Prompt for user-uploaded image: 通用图片描述，不假定是屏幕。"""
    return get_prompt("vlm.upload_image")


async def _call_vlm(
    preset: dict,
    image_b64: str,
    session_id: str = "",
    ocr_text: str = "",
    image_type: str = "screenshot",
) -> str:
    """Call the vision-capable model to describe the image.

    image_type: "screenshot" = 屏幕截图（应用/窗口/用户活动/文字谨慎）；"upload" = 用户上传的任意图片（通用描述）。
    Uses the OpenAI-compat SDK (same as all other LLM calls in this project).
    Gemini via the OpenAI-compat endpoint supports image_url content blocks.

    If ocr_text is provided (e.g. from a future OCR step), it is prepended to the
    prompt so the model can use exact text instead of guessing from the image.
    """
    from openai import AsyncOpenAI

    api_key = preset.get("api_key", "sk-placeholder")
    base_url = preset.get("base_url")
    model = preset.get("model", "gemini-2.5-flash")

    # Detect MIME type from base64 header
    mime = "image/jpeg"
    if image_b64.startswith("iVBORw0KGgo"):
        mime = "image/png"
    elif image_b64.startswith("/9j/"):
        mime = "image/jpeg"

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url or None,
    )

    _ocr_prefix = ""
    if ocr_text and ocr_text.strip():
        _ocr_prefix = Template(get_prompt(
            "vlm.ocr_prefix",
            default="以下为 OCR 识别的屏幕文字（供参考，涉及具体字句请以之为准）：\n$ocr_text\n\n",
        )).safe_substitute(ocr_text=ocr_text.strip())

    if image_type == "upload":
        _text_prompt = _upload_image_prompt()
    else:
        _text_prompt = _screen_prompt(_ocr_prefix)

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                },
                {
                    "type": "text",
                    "text": _text_prompt,
                },
            ],
        }
    ]

    # Screenshots (incl. ASE) and uploads both use a higher token limit.
    _max_tokens = 5000

    # Log call — skip the base64 image block to avoid huge log entries
    _log_messages = [{"role": "user", "content": f"[image ~{len(image_b64)*3//4} bytes, {mime}] {_text_prompt}"}]
    log_secondary_llm_call(
        role="vlm",
        messages=_log_messages,
        model=model,
        gen_kwargs={"max_tokens": _max_tokens},
        session_id=session_id,
    )

    _t0 = time.time()
    result = await client.chat.completions.create(
        model=model,
        messages=messages,
        stream=False,
        max_tokens=_max_tokens,
    )
    description = result.choices[0].message.content.strip()
    log_secondary_llm_response(
        role="vlm",
        response=description,
        model=model,
        session_id=session_id,
        duration_ms=int((time.time() - _t0) * 1000),
    )
    return description
