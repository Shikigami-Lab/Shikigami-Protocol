"""Same-origin host workspace controls. The parent app includes this router."""
from contextlib import asynccontextmanager, contextmanager
from urllib.parse import quote
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from src.agent import workspace
from src.api.agent import _runtime, same_origin
from src.agent.workspace_catalog import FileDescription

router = APIRouter(prefix="/api/agent/workspace", dependencies=[Depends(same_origin)])


@contextmanager
def _errors():
    try:
        yield
    except workspace.WorkspaceLimitError as error:
        raise HTTPException(413, str(error)) from None
    except workspace.WorkspaceChangedError as error:
        raise HTTPException(409, str(error)) from None
    except FileNotFoundError:
        raise HTTPException(404, "Workspace file or version not found") from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    except OSError:
        raise HTTPException(409, "Workspace file is unavailable or changed; retry when the task stops") from None


def _profile(request, profile_id):
    workspace.validate_profile_id(profile_id)
    session = request.app.state.session_manager.get_by_id(profile_id)
    if session is None or session.profile_id != profile_id:
        raise HTTPException(404, "Character not found")
    runtime = _runtime(request)
    if getattr(runtime, "workspaces", None) is None:
        raise HTTPException(503, "Workspace storage is not initialized")
    return runtime


@contextmanager
def _mutation(runtime, profile_id):
    # Same lock as runtime.prepare(). No await while held: admission cannot
    # race the busy check/write. A terminal event may precede worker teardown.
    with runtime._state_lock:
        cleanup_pending = getattr(runtime, "cleanup_pending", None)
        if cleanup_pending and cleanup_pending(profile_id):
            raise HTTPException(409, "Workspace task cleanup is pending; retry after it stops")
        if any(run.profile_id == profile_id and
               (not run.terminal or (getattr(run, "task", None) is not None and not run.task.done())
                or (getattr(run, "process", None) is not None and run.process.returncode is None))
               for run in runtime._runs.values()):
            raise HTTPException(409, "Stop this character's workspace task before changing files")
        yield


@router.get("/files")
async def files(request: Request, profile_id: str):
    with _errors():
        runtime = _profile(request, profile_id)
        inventory = runtime.catalog.inventory(profile_id)
        total = sum(row["size"] for row in inventory["files"])
        return {**inventory, "total_bytes": total,
                "total_bytes_is_lower_bound": inventory["truncated"],
                "over_byte_limit": True if total > workspace.MAX_WORKSPACE_BYTES else
                    None if inventory["truncated"] else False}


class Metadata(FileDescription):
    profile_id: str = Field(min_length=1, max_length=256)


@router.patch('/metadata')
async def metadata(request: Request, body: Metadata):
    with _errors():
        runtime = _profile(request, body.profile_id)
        with _mutation(runtime, body.profile_id):
            file = runtime.catalog.describe(body.profile_id, **body.model_dump(exclude={'profile_id'}))
        return {'ok': True, 'file': file}


@router.get("/snapshot")
async def snapshot_manifest(request: Request, profile_id: str, snapshot_id: str):
    with _errors():
        return _profile(request, profile_id).workspaces.snapshot_manifest(profile_id, snapshot_id)


def _preview_type(path, data):
    # Only explicit inert raster formats get image Content-Type; never SVG.
    suffix = path.rsplit(".", 1)[-1].lower()
    if suffix == "png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if suffix in ("jpg", "jpeg") and data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if suffix == "gif" and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if suffix == "webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    try:
        data.decode("utf-8")
        if b"\x00" not in data:
            return "text/plain"
    except UnicodeDecodeError:
        pass
    return "application/octet-stream"


@router.get("/file")
async def file(request: Request, profile_id: str, path: str, download: bool = False):
    with _errors():
        data = _profile(request, profile_id).workspaces.read_bytes(profile_id, path)
        media_type = "application/octet-stream" if download else _preview_type(path, data)
        disposition = "attachment" if download or media_type == "application/octet-stream" else "inline"
        return Response(data, media_type=media_type, headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(path.rsplit('/', 1)[-1], safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'self'",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Cache-Control": "no-store",
        })


@asynccontextmanager
async def _upload_form(request):
    # FastAPI File/Form parameters parse and spool the entire request before
    # entering the route. Bound the incoming stream as well as the final file.
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(415, "Upload requires multipart/form-data")
    exceeded = False

    async def bounded_stream():
        nonlocal exceeded
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > workspace.MAX_FILE_BYTES + 64 * 1024:
                exceeded = True
                raise MultiPartException("Upload exceeds the workspace request limit")
            yield chunk

    parser = MultiPartParser(request.headers, bounded_stream(), max_files=1, max_fields=2)
    try:
        form = await parser.parse()
        fields = list(form.multi_items())
        if (len(fields) != len(form) or set(form) - {"profile_id", "path", "file"}
                or not isinstance(form.get("file"), UploadFile)
                or not isinstance(form.get("profile_id"), str)
                or ("path" in form and not isinstance(form["path"], str))):
            raise HTTPException(422, "Upload requires profile_id, file, and optionally path")
        yield form
    except MultiPartException as error:
        raise HTTPException(413 if exceeded else 400, error.message) from None
    finally:
        # Also cover malformed/disconnected streams before parse() returns a
        # FormData owner. Starlette only cleans these itself on MultiPartException.
        for handle in parser._files_to_close_on_error:
            handle.close()


def _versioned_write(store, profile_id, path, data, operation, *, snapshot_before):
    """Caller holds the runtime mutation lock through both snapshots and write."""
    operation_id = operation + '-' + uuid.uuid4().hex
    before_id = store.snapshot(profile_id, operation_id + '-before') if snapshot_before else None
    before = store.snapshot_manifest(profile_id, before_id) if before_id else None
    result = store.write_file(profile_id, path, data)
    after_id = store.snapshot(profile_id, operation_id + '-after')
    after = store.snapshot_manifest(profile_id, after_id)
    partial = not after['complete'] or (before is not None and not before['complete'])
    return {**result, 'before_snapshot_id': before_id, 'snapshot_id': after_id,
            'snapshot_complete': after['complete'],
            'before_snapshot_complete': before['complete'] if before else None,
            'workspace_warning': 'Some files were not included in the saved versions; inspect the snapshot details.' if partial else ''}


@router.post("/upload")
async def upload(request: Request):
    with _errors():
        async with _upload_form(request) as form:
            profile_id, file = form["profile_id"], form["file"]
            runtime = _profile(request, profile_id)
            target = form.get("path", file.filename)
            workspace._parts(target)
            # Reject busy before reading, and check again after the await.
            with _mutation(runtime, profile_id):
                pass
            data = await file.read(workspace.MAX_FILE_BYTES + 1)
            if len(data) > workspace.MAX_FILE_BYTES:
                raise workspace.WorkspaceLimitError("File exceeds 10 MB workspace limit")
            with _mutation(runtime, profile_id):
                try:
                    runtime.workspaces.read_file(profile_id, target)
                    exists = True
                except FileNotFoundError:
                    exists = False
                except workspace.WorkspaceLimitError:
                    exists = True  # Oversized current files remain repairable.
                return _versioned_write(runtime.workspaces, profile_id, target, data,
                                        'upload', snapshot_before=exists)


@router.get("/versions")
async def versions(request: Request, profile_id: str, path: str):
    with _errors():
        return {"versions": _profile(request, profile_id).workspaces.versions(profile_id, path)}


class Restore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=256)
    path: str = Field(min_length=1, max_length=1024)
    version_id: str = Field(min_length=1, max_length=64)


@router.post("/restore")
async def restore(request: Request, body: Restore):
    with _errors():
        runtime = _profile(request, body.profile_id)
        with _mutation(runtime, body.profile_id):
            # Pin the bytes before the before-snapshot prunes old versions.
            data = runtime.workspaces.read_version(body.profile_id, body.path, body.version_id)
            return _versioned_write(runtime.workspaces, body.profile_id, body.path, data,
                                    'restore', snapshot_before=True)
