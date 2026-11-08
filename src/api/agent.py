"""UI controls for host-owned agent tasks; no model-facing control endpoint."""
import asyncio
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from src.agent.settings import normalize_settings, save_settings

def _same_origin(request: Request):
    from urllib.parse import urlsplit
    origin = request.headers.get('origin')
    if origin and (urlsplit(origin).scheme != request.url.scheme or urlsplit(origin).netloc != request.url.netloc):
        raise HTTPException(403, 'Open agent controls from this application')


router = APIRouter(prefix="/api/agent", dependencies=[Depends(_same_origin)])


def _runtime(request):
    runtime = getattr(request.app.state, "agent_runtime", None)
    if runtime is None:
        raise HTTPException(503, "Tool runtime is not initialized")
    return runtime


class DailyReportSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time: str = Field(strict=True, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    instructions: str = Field(strict=True, min_length=1, max_length=1000)
    enabled: bool = Field(strict=True)


def _require_loaded_report_profile(request, profile_id):
    from src.agent.daily_report_delivery import loaded_report_session
    if loaded_report_session(request.app.state.session_manager, profile_id) is None:
        raise HTTPException(404, "Character is not loaded in chat")


@router.get("/daily-report")
async def get_daily_report(request: Request, profile_id: str = Query(min_length=1, max_length=256)):
    runtime = _runtime(request)
    _require_loaded_report_profile(request, profile_id)
    return {"report": await asyncio.to_thread(runtime.daily_reports.get, profile_id)}


@router.put("/daily-report")
async def configure_daily_report(request: Request, body: DailyReportSettings,
                                 profile_id: str = Query(min_length=1, max_length=256)):
    runtime = _runtime(request)
    _require_loaded_report_profile(request, profile_id)
    try:
        with runtime._state_lock:
            report = runtime.daily_reports.configure(profile_id, body.time, body.instructions, body.enabled)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return {"report": report}


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = Field(default=None, strict=True)
    model_preset: Optional[str] = Field(default=None, strict=True)
    workspace_enabled: Optional[bool] = Field(default=None, strict=True)
    code_enabled: Optional[bool] = Field(default=None, strict=True)
    web_enabled: Optional[bool] = Field(default=None, strict=True)
    history_enabled: Optional[bool] = Field(default=None, strict=True)
    memory_enabled: Optional[bool] = Field(default=None, strict=True)
    todo_enabled: Optional[bool] = Field(default=None, strict=True)
    autonomy_enabled: Optional[bool] = Field(default=None, strict=True)
    autonomy_interval_seconds: Optional[int] = Field(default=None, strict=True, ge=60, le=86400)
    autonomy_max_requests_per_hour: Optional[int] = Field(default=None, strict=True, ge=1, le=128)
    max_model_requests: Optional[int] = Field(default=None, strict=True, ge=1, le=32)
    timeout_seconds: Optional[int] = Field(default=None, strict=True, ge=10, le=600)


@router.get("/settings")
async def settings(request: Request):
    return _runtime(request).settings


class ViewState(BaseModel):
    model_config = ConfigDict(extra='forbid')
    group_chat: bool = Field(strict=True)


@router.post('/view')
async def view_state(request: Request, body: ViewState):
    # Like session selection, the most recently selected view is app-wide.
    # Only new background admission is gated; captured tasks keep their owner.
    _runtime(request).autonomy.group_view = body.group_chat
    return {'group_chat': body.group_chat}


@router.patch("/settings")
async def patch_settings(request: Request, body: SettingsPatch):
    runtime = _runtime(request)
    changes = body.model_dump(exclude_unset=True)
    try:
        value = normalize_settings({**runtime.settings, **changes})
        if changes.get('model_preset'):
            runtime.tool_model_preset(value['model_preset'])
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    save_settings(runtime.root / "config" / "app.yaml", value)
    request.app.state.config.agent = value
    # Defaults may change without revoking a persona's explicit opt-in.
    for run in list(runtime._runs.values()):
        if not run.terminal and not runtime.permissions_valid(run):
            await runtime.cancel(run.id, run.profile_id)
    return value


@router.get("/status")
async def status(request: Request):
    return await _runtime(request).status()


@router.post("/check")
async def check(request: Request):
    return await _runtime(request).status(refresh=True)


def _public_run(runtime, row):
    binding = {"run_id": row['run_id'], "request_id": row['run_id'],
               "profile_id": row['profile_id'], "session_id": row['profile_id']}
    events = []
    artifacts = []
    workspace_warning = ''
    for item in row['events']:
        event = {**item, **binding, "sequence": item['seq']}
        value = item.get('result') or {}
        if value.get('terminal_type') or item['type'] in ('completed', 'cancelled', 'failed'):
            artifacts = value.get('artifacts', [])
            workspace_warning = value.get('workspace_warning', '')
            event['workspace_warning'] = workspace_warning
            event['artifacts'] = artifacts
            event['type'] = row['state'] if row['state'] in ('completed', 'cancelled', 'failed') else 'status'
            if row.get('error'):
                event['error'] = row['error']
        if item['type'] == 'tool_call':
            import json
            event['arguments'] = json.dumps(value.get('arguments', {}), ensure_ascii=False)
        elif item['type'] == 'tool_result':
            event.update(is_error=value.get('is_error', False), content=[{'type': 'text', 'text': item.get('summary', '')}])
        elif item['type'] == 'approval_requested':
            event.update(value)
        events.append(event)
    return {**row, **binding, "id": row['run_id'], "events": events, 'artifacts': artifacts, 'workspace_warning': workspace_warning}


@router.get("/runs")
async def runs(request: Request, profile_id: str, query: str = Query(default="", max_length=500)):
    runtime = _runtime(request)
    if (not profile_id or profile_id in ('.', '..') or any(c in profile_id for c in ('/', '\\', ':'))
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in profile_id) or len(profile_id) > 256):
        raise HTTPException(400, 'Invalid character identity')
    session = request.app.state.session_manager.get_by_id(profile_id)
    if session is None:
        raise HTTPException(404, "Character not found")
    if query.strip():
        from src.agent.task_search import TaskSearchArgs, search_tasks
        args = TaskSearchArgs(query=query, limit=10)
        def search():
            revision = runtime.store.revision(session.profile_id)
            result = search_tasks(runtime.store, session, args, include_artifacts=True)
            matches = []
            for match in result['matches']:
                row = runtime.store.get_run(match['run_id'], session.profile_id)
                if row and not row['stale']:
                    matches.append({**_public_run(runtime, row), 'request_excerpt': match['request_excerpt'],
                                    'task_summary': match['summary']})
            current_revision = runtime.store.revision(session.profile_id)
            if revision != current_revision:
                return {'runs': [], 'revision': current_revision}
            return {'runs': matches, 'scan_limited': result['scan_limited'],
                    'has_more_matches': result['has_more_matches'], 'revision': revision}
        return await asyncio.to_thread(search)
    rows = runtime.store.list_runs(session.profile_id)
    revision = runtime.store.revision(session.profile_id)
    return {"runs": [_public_run(runtime, row) for row in rows if not row['stale'] and row['revision'] == revision],
            "revision": revision}


class RunControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=256, pattern=r"^[^/\\:\x00-\x1f\x7f-\x9f]+$")


class Approval(RunControl):
    approval_id: str = Field(min_length=1, max_length=256)
    outcome: Literal['allowed-once', 'rejected']


def _owned(runtime, run_id, profile_id):
    row = runtime.store.get_run(run_id, profile_id)
    if row is None or row['stale']:
        raise HTTPException(404, "Task not found in this character")
    return row


@router.post("/runs/{run_id}/cancel")
async def cancel(request: Request, run_id: str, body: RunControl):
    runtime = _runtime(request)
    _owned(runtime, run_id, body.profile_id)
    accepted = await runtime.cancel(run_id, body.profile_id)
    return {"accepted": accepted, "state": runtime.store.get_run(run_id, body.profile_id)['state']}


@router.post("/runs/{run_id}/approval")
async def approve(request: Request, run_id: str, body: Approval):
    runtime = _runtime(request)
    _owned(runtime, run_id, body.profile_id)
    try:
        accepted = await runtime.answer(run_id, body.profile_id, body.approval_id, body.outcome)
    except ValueError:
        accepted = False
    return {"accepted": accepted}
