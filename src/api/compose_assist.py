"""User reply drafts and explicit continuation of the last character reply."""
import time
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.config.effective_config import get_effective_max_history_turns
from src.config.profile_loader import ProfileLoader
from src.core.session import Session
from src.core.broadcast import broadcast
from src.llm.registry import get_provider
from src.prompt.pipeline import build_messages

router = APIRouter(prefix="/api/chat", tags=["chat-writing"])


class ComposeAssistRequest(BaseModel):
    mode: Literal["continue", "reply"]
    session_id: str = Field(min_length=1, max_length=256)
    group_id: Optional[str] = Field(default=None, max_length=128)
    target_message_id: Optional[int] = Field(default=None, ge=1)
    target_profile_id: Optional[str] = Field(default=None, max_length=256)
    draft: str = Field(default="", max_length=12000)
    instruction: str = Field(default="", max_length=1000)


def _latest_assistant(store):
    for message in reversed(store.get_page_with_id(200, 0)):
        if message.get("role") == "assistant" and (message.get("content") or "").strip():
            return message
    return None


def _task_prompt(body: ComposeAssistRequest, target: dict) -> str:
    instruction = body.instruction.strip()
    if body.mode == "continue":
        return (
            "Continue your previous character reply from where it ended, as the same character. "
            "Write only the next part. Preserve its language, voice and scene; do not repeat it, "
            "introduce a user action or add a preface.\n\n"
            f"Previous character reply:\n{target['content']}"
        )
    if body.target_message_id is None:
        raise HTTPException(status_code=422, detail="A character message is required to draft a reply")
    return (
        "Writing command: draft a message the user could send in reply to the latest character message in this chat. "
        "Write as the user, never as the character. Return only the proposed message, with no preface or explanation.\n\n"
        + (f"User's existing draft or intended points:\n{body.draft}\n\n" if body.draft.strip() else "")
        + (f"Optional instruction: {instruction}" if instruction else "")
    )


def _group_context(request: Request, body: ComposeAssistRequest, target: dict):
    manager = getattr(request.app.state, "group_manager", None)
    group = manager.get_group(body.group_id) if manager and body.group_id else None
    if group is None:
        raise HTTPException(status_code=404, detail="Chat group was not found")
    store = manager.get_conversation_store(body.group_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Chat group history is unavailable")
    if target is None or target.get("id") != body.target_message_id:
        raise HTTPException(status_code=409, detail="The character message changed; reopen the command")

    participants = []
    profile_loader = ProfileLoader()
    for participant in group.get("participants") or []:
        profile_id = (participant.get("profile_id") or "").strip()
        if not profile_id:
            continue
        profile = profile_loader.load(profile_id) or {}
        name = (participant.get("name") or profile.get("display_name") or profile_id).strip()
        participants.append((profile_id, name))

    sender = (target.get("sender") or "").strip()
    matches = [(profile_id, name) for profile_id, name in participants if name == sender]
    if body.target_profile_id:
        matches = [row for row in matches if row[0] == body.target_profile_id]
    if len(matches) != 1:
        raise HTTPException(status_code=409, detail="Could not identify the character who wrote that message")
    profile_id, display_name = matches[0]
    session = request.app.state.session_manager.get_by_id(profile_id)
    if session is None:
        session = Session.from_profile(profile_id, display_name)
        session.load_runtime_state()

    group_name = (group.get("display_name") or body.group_id).strip()
    all_names = ", ".join(name for _, name in participants)
    extra_system = (
        (group.get("system_hint") or "").strip()
        + f"\n\nCurrent group participants: {all_names}. You are {display_name}."
    ).strip()
    return session, store, {
        "group_reply_as_sender": display_name,
        "group_user_name": (group.get("user_name") or request.app.state.config.user_name or "用户").strip(),
        "extra_system": extra_system,
        "current_group_id": body.group_id,
        "current_group_gname": group_name,
    }


@router.post("/compose-assist")
async def compose_assist(request: Request, body: ComposeAssistRequest):
    """Use the active model to draft a user reply or continue the character."""
    config = request.app.state.config
    preset = config.get_active_llm_preset()
    if not preset or not preset.get("base_url") or not preset.get("model"):
        raise HTTPException(status_code=503, detail="Configure a chat model before using writing assistance")

    target = None
    group_options = {}
    if body.group_id:
        if body.mode == "reply" or body.target_message_id is not None:
            manager = getattr(request.app.state, "group_manager", None)
            group = manager.get_group(body.group_id) if manager else None
            store = manager.get_conversation_store(body.group_id) if group else None
            if store is None:
                raise HTTPException(status_code=404, detail="Chat group history is unavailable")
            target = _latest_assistant(store)
            session, store, group_options = _group_context(request, body, target)
        else:
            raise HTTPException(status_code=422, detail="A character message is required for continuation")
    else:
        session = request.app.state.session_manager.get_by_id(body.session_id)
        if session is None or session.id != body.session_id:
            raise HTTPException(status_code=404, detail="Chat session was not found")
        store = session.conversation_store
        target = _latest_assistant(store)
        if target is None or target.get("id") != body.target_message_id:
            raise HTTPException(status_code=409, detail="The character message changed; reopen the command")

    anchor = store.get_page_with_id(1, 0)
    anchor_id = anchor[-1]["id"] if anchor else None
    if body.mode == "continue" and anchor_id != target["id"]:
        raise HTTPException(status_code=409, detail="The latest message must be a character reply to continue it")

    from src.api.memory import _get_or_create_manager
    _get_or_create_manager(request, session.profile_id)
    prompt = _task_prompt(body, target)
    writing_role = (
        "Continue speaking as the character. The request is to extend your previous reply, not to draft a user reply."
        if body.mode == "continue" else
        "You are helping the user prepare an unsent chat draft. Write from the user's perspective; "
        "do not answer as the character, do not claim the draft was sent, and do not add commentary."
    )
    group_options["extra_system"] = (
        (group_options.get("extra_system") or "")
        + "\n\n" + writing_role
    ).strip()
    messages = build_messages(
        session,
        prompt,
        store,
        n_history_turns=get_effective_max_history_turns(request.app, session.profile_id),
        preset=preset,
        app=request.app,
        **group_options,
    )

    try:
        result = (await get_provider(preset).chat(messages)).strip()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Writing assistance could not reach the configured model") from exc
    if not result or result.startswith("[LLM Error:"):
        raise HTTPException(status_code=502, detail="The model did not return a usable draft")
    if body.mode == "continue":
        disconnected = await request.is_disconnected()
        current = store.get_page_with_id(1, 0)
        if disconnected or not current or current[-1]["id"] != anchor_id:
            raise HTTPException(status_code=409, detail="The chat changed or continuation was cancelled; no reply was saved")
        sender = target.get("sender") or session.display_name or ""
        message_id = store.append("assistant", result, sender=sender)
        await broadcast.push(f"group:{body.group_id}" if body.group_id else session.id, {
            "type": "new_message", "message_id": message_id, "role": "assistant",
            "content": result, "sender": sender, "timestamp": time.time(), "client_id": "",
        })
        return {"text": result, "mode": body.mode, "message_id": message_id}
    return {"text": result, "mode": body.mode}
