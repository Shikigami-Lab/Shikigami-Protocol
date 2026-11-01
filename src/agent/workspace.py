"""Bounded host storage, not model filesystem tools.

Mount ONLY directory(profile_id) into the worker. Profile metadata and the
legacy root's versions.sqlite3 must stay outside that mount. Call snapshot before and after a task;
the runtime must serialize writers for a profile (including upload/restore).
read_file returns a validated Path, not a durable capability: consumers that
read content must use read_bytes, which pins parents and validates open handles.
"""
from contextlib import contextmanager
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_WORKSPACE_BYTES = 50 * 1024 * 1024
MAX_FILES = 500
MAX_ENTRIES = 2000
MAX_DEPTH = 16
MAX_SNAPSHOTS = 10
MAX_GLOBAL_SNAPSHOTS = 128
MAX_VERSION_BYTES = 256 * 1024 * 1024
_PRIVATE = {"private", "runtime", "runtime_state", "runtime_state.json", "profiles",
            "versions.sqlite3", "versions.sqlite3-journal", "__pycache__"}
_DEVICE = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\.|$)", re.I)


class WorkspaceLimitError(ValueError):
    """An operation exceeded a hard storage or traversal budget."""


class WorkspaceChangedError(ValueError):
    """A writer changed the file while a bounded preview was being read."""


def validate_profile_id(profile_id):
    if (not isinstance(profile_id, str) or not profile_id or len(profile_id) > 256
            or profile_id in (".", "..") or any(c in profile_id for c in "/\\:")
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in profile_id)):
        raise ValueError("Invalid character identity")
    return profile_id


def _parts(path):
    if not isinstance(path, str) or not path or len(path) > 1024:
        raise ValueError("Invalid workspace path")
    parts = path.split("/")
    if len(parts) > MAX_DEPTH:
        raise WorkspaceLimitError("Workspace path is too deep")
    for part in parts:
        if (not part or len(part) > 255 or part.startswith(".") or part.endswith((".", " "))
                or part.casefold() in _PRIVATE or _DEVICE.match(part)
                or any(c in part for c in '\\:<>"|?*')
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in part)):
            raise ValueError("Unsafe or private workspace path")
    return parts


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _regular(info, *, check_size=True):
    if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Workspace entry is not an unlinked regular file")
    if check_size and info.st_size > MAX_FILE_BYTES:
        raise WorkspaceLimitError("File exceeds 10 MB workspace limit")


if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _FileInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("created", wintypes.FILETIME),
                    ("accessed", wintypes.FILETIME), ("written", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    _kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL

    def _win_open(path, directory=False):
        # No SHARE_DELETE: ancestors cannot be renamed/replaced while in use.
        # OPEN_REPARSE_POINT: never follow a file/link swapped after validation.
        handle = _kernel.CreateFileW(str(path), 0x80 if directory else 0x80000000,
                                     3 if directory else 1, None, 3, 0x02200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _FileInfo()
        try:
            if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
                raise ValueError("Workspace links and reparse points are forbidden")
        except BaseException:
            _kernel.CloseHandle(handle)
            raise
        return handle


@contextmanager
def _directory(path, *, create=False):
    """Walk without following links; keep ancestors pinned until the I/O ends."""
    handles = []
    current = Path(path.anchor)
    try:
        if os.name != "nt":
            handles.append(os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for part in path.parts[1:]:
            current = current / part
            if create:
                try:
                    if os.name == "nt":
                        current.mkdir(mode=0o700)
                    else:
                        os.mkdir(part, mode=0o700, dir_fd=handles[-1])
                except FileExistsError:
                    pass
            if os.name == "nt":
                handles.append(_win_open(current, directory=True))
            else:
                try:
                    handles.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                           dir_fd=handles[-1]))
                except (NotADirectoryError, OSError) as error:
                    if isinstance(error, FileNotFoundError):
                        raise
                    raise ValueError("Unsafe workspace directory") from error
        yield None if os.name == "nt" else handles[-1]
    finally:
        for handle in reversed(handles):
            if os.name == "nt":
                _kernel.CloseHandle(handle)
            else:
                os.close(handle)


def _lstat(path, parent_fd):
    return path.lstat() if parent_fd is None else os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)


@contextmanager
def _open_file(path, *, check_size=True):
    with _directory(path.parent) as parent_fd:
        before = _lstat(path, parent_fd)
        _regular(before, check_size=check_size)
        if os.name == "nt":
            handle = _win_open(path)
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except BaseException:
                _kernel.CloseHandle(handle)
                raise
        else:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(fd, "rb") as stream:
            after = os.fstat(stream.fileno())
            _regular(after, check_size=check_size)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("Workspace file changed during access")
            yield stream, after


class WorkspaceStore:
    def __init__(self, root, *, profiles_root=None, migration_allowed=None):
        self.root = Path(os.path.abspath(root))
        # None retains the opaque standalone layout for existing callers.
        self.profiles_root = None if profiles_root is None else Path(os.path.abspath(profiles_root))
        self.migration_allowed = migration_allowed
        self._lock = threading.RLock()
        with _directory(self.root, create=True):
            pass
        with self._database() as db:
            db.executescript("""
                PRAGMA auto_vacuum=FULL;
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY, profile TEXT NOT NULL, run_id TEXT NOT NULL,
                    created REAL NOT NULL, size INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS snapshots_profile ON snapshots(profile, created);
                CREATE TABLE IF NOT EXISTS files (
                    snapshot TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                    path TEXT NOT NULL, modified REAL NOT NULL, size INTEGER NOT NULL,
                    data BLOB NOT NULL, PRIMARY KEY(snapshot, path));
            """)
            if "manifest" not in {row[1] for row in db.execute("PRAGMA table_info(snapshots)")}:
                db.execute("ALTER TABLE snapshots ADD COLUMN manifest TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def _database(self):
        # This host-only directory must never be mounted into a model worker.
        with _directory(self.root):
            for name in ("versions.sqlite3", "versions.sqlite3-journal", "versions.sqlite3-wal", "versions.sqlite3-shm"):
                path = self.root / name
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Unsafe workspace version database")
            db = sqlite3.connect(self.root / "versions.sqlite3", timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                with db:
                    yield db
            finally:
                db.close()

    def directory(self, profile_id):
        key = hashlib.sha256(validate_profile_id(profile_id).encode("utf-8")).hexdigest()
        legacy = self.root / "files" / key
        if self.profiles_root is None:
            with _directory(legacy, create=True):
                pass
            return legacy
        # The old ID was only hashed. It is now a host directory component, so
        # also reject Windows aliases, devices, ADS and wildcard characters.
        _parts(profile_id)
        host = self.profiles_root / profile_id
        target = host / "workspace"
        marker = host / ".workspace_migration.json"
        with self._lock, _directory(host, create=True):
            record = self._migration_record(marker, profile_id)
            if record and record["state"] == "complete":
                with _directory(target):
                    return target
            legacy_info = self._entry_info(legacy)
            if legacy_info is not None:
                if self.migration_allowed is not None and not self.migration_allowed(profile_id):
                    raise ValueError("Workspace migration blocked by active work or pending cleanup")
                with _directory(legacy):
                    return self._migrate(profile_id, legacy, target, marker, record)
            if record:
                raise ValueError("Incomplete workspace migration has lost its legacy source")
            with _directory(target, create=True):
                pass
            self._save_migration_record(marker, {"version": 1, "profile_id": profile_id,
                                                "state": "complete"})
            return target

    @staticmethod
    def _entry_info(path):
        try:
            with _directory(path.parent) as fd:
                return _lstat(path, fd)
        except FileNotFoundError:
            return None

    @staticmethod
    def _migration_record(path, profile_id):
        try:
            with _open_file(path, check_size=False) as (stream, _):
                data = stream.read(4097)
        except FileNotFoundError:
            return None
        try:
            record = json.loads(data) if len(data) <= 4096 else None
        except (ValueError, UnicodeError) as error:
            raise ValueError("Invalid workspace migration marker") from error
        if (not isinstance(record, dict) or record.get("version") != 1
                or record.get("profile_id") != profile_id
                or record.get("state") not in ("prepared", "complete")
                or (record["state"] == "prepared" and
                    not re.fullmatch(r"[0-9a-f]{64}", str(record.get("digest", ""))))):
            raise ValueError("Invalid workspace migration marker")
        return record

    @staticmethod
    def _save_migration_record(path, record):
        # Host-only atomic marker; never stored in the directory mounted by a worker.
        with _directory(path.parent) as fd:
            temp = path.parent / (".workspace-marker-" + uuid.uuid4().hex)
            descriptor = os.open(temp if fd is None else temp.name,
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                                 0o600, **({} if fd is None else {"dir_fd": fd}))
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(json.dumps(record, sort_keys=True).encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                if fd is None:
                    os.replace(temp, path)
                else:
                    os.replace(temp.name, path.name, src_dir_fd=fd, dst_dir_fd=fd)
                    os.fsync(fd)
            finally:
                try:
                    os.unlink(temp if fd is None else temp.name, **({} if fd is None else {"dir_fd": fd}))
                except FileNotFoundError:
                    pass

    @staticmethod
    def _migration_stamp(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    @classmethod
    def _migration_bytes(cls, path):
        with _open_file(path) as (stream, before):
            data = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
            _regular(after)
            if len(data) > MAX_FILE_BYTES:
                raise WorkspaceLimitError("Legacy workspace file exceeds migration byte limit")
            if len(data) != before.st_size or cls._migration_stamp(before) != cls._migration_stamp(after):
                raise WorkspaceChangedError("Legacy workspace changed during migration")
            return data, cls._migration_stamp(after)

    @classmethod
    def _migration_tree(cls, base):
        """Complete, strict inventory: migration must never silently omit an entry."""
        contents, identities = {}, {}
        pending = [base]
        count = total = entries = 0
        while pending:
            directory = pending.pop()
            with _directory(directory) as fd:
                info = directory.stat() if fd is None else os.fstat(fd)
                identities[directory.relative_to(base).as_posix()] = cls._migration_stamp(info)
                with os.scandir(directory if fd is None else fd) as scan:
                    for entry in scan:
                        entries += 1
                        if entries > MAX_ENTRIES:
                            raise WorkspaceLimitError("Legacy workspace exceeds migration entry limit")
                        path = directory / entry.name
                        relative = path.relative_to(base).as_posix()
                        parts = _parts(relative)
                        info = _lstat(path, fd)
                        if _linked(info):
                            raise ValueError("Workspace links and reparse points are forbidden during migration")
                        if stat.S_ISDIR(info.st_mode):
                            if len(parts) >= MAX_DEPTH:
                                raise WorkspaceLimitError("Legacy workspace exceeds migration depth limit")
                            contents[relative] = ["directory"]
                            pending.append(path)
                        else:
                            _regular(info)
                            count += 1
                            total += info.st_size
                            if count > MAX_FILES or total > MAX_WORKSPACE_BYTES:
                                raise WorkspaceLimitError("Legacy workspace exceeds migration file or byte limit")
                            data, stamp = cls._migration_bytes(path)
                            # Windows path stat may expose creation time as
                            # ctime, while fstat reports the actual change time.
                            # Compare their shared fields here; all read/copy
                            # stability checks compare handle stamps in full.
                            if stamp[:-1] != cls._migration_stamp(info)[:-1]:
                                raise WorkspaceChangedError("Legacy workspace changed during migration")
                            contents[relative] = ["file", len(data), hashlib.sha256(data).hexdigest()]
                            identities[relative] = stamp
        return contents, identities

    @staticmethod
    def _migration_digest(contents):
        return hashlib.sha256(json.dumps(contents, sort_keys=True).encode("utf-8")).hexdigest()

    def _migrate(self, profile_id, legacy, target, marker, record):
        if self._entry_info(target) is not None:
            with _directory(target) as fd:
                with os.scandir(target if fd is None else fd) as scan:
                    nonempty = next(scan, None) is not None
            if nonempty:
                if not record and not self._migration_tree(legacy)[0]:
                    self._save_migration_record(marker, {"version": 1, "profile_id": profile_id,
                                                        "state": "complete"})
                    return target
                # Only a durable prepared transaction can identify a copy that
                # was published just before interruption. Never merge trees.
                if (not record or record["state"] != "prepared"
                        or self._migration_digest(self._migration_tree(target)[0]) != record["digest"]):
                    raise ValueError("Workspace collision: legacy and destination both contain data")
                self._save_migration_record(marker, {**record, "state": "complete"})
                return target

        before, identities = self._migration_tree(legacy)
        stage = target.parent / (".workspace-stage-" + uuid.uuid4().hex)
        created_files, created_directories = [], []
        try:
            with _directory(stage, create=True):
                pass
            for relative, entry in sorted(before.items()):
                destination = stage / relative
                if entry[0] == "directory":
                    with _directory(destination, create=True):
                        pass
                    created_directories.append(destination)
                else:
                    data, stamp = self._migration_bytes(legacy / relative)
                    if (stamp != identities[relative]
                            or ["file", len(data), hashlib.sha256(data).hexdigest()] != entry):
                        raise WorkspaceChangedError("Legacy workspace changed during migration")
                    with _directory(destination.parent) as fd:
                        descriptor = os.open(destination if fd is None else destination.name,
                                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                                             0o600, **({} if fd is None else {"dir_fd": fd}))
                        created_files.append(destination)
                        with os.fdopen(descriptor, "wb") as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                        # Copying must not make an old note look newly edited.
                        if fd is None:
                            # Windows cannot utime with follow_symlinks=False;
                            # pin the validated file against replacement instead.
                            with _open_file(destination):
                                os.utime(destination, ns=(stamp[-2], stamp[-2]))
                        else:
                            os.utime(destination.name, ns=(stamp[-2], stamp[-2]),
                                     follow_symlinks=False, dir_fd=fd)
            if self._migration_tree(stage)[0] != before or self._migration_tree(legacy) != (before, identities):
                raise WorkspaceChangedError("Legacy workspace changed or copy verification failed during migration")
            prepared = {"version": 1, "profile_id": profile_id, "state": "prepared",
                        "digest": self._migration_digest(before)}
            self._save_migration_record(marker, prepared)
            with _directory(target.parent) as fd:
                # rmdir accepts only an empty directory; a concurrent write fails
                # closed rather than overwriting or merging either workspace.
                if self._entry_info(target) is not None:
                    os.rmdir(target if fd is None else target.name, **({} if fd is None else {"dir_fd": fd}))
                if fd is None:
                    os.rename(stage, target)
                else:
                    os.rename(stage.name, target.name, src_dir_fd=fd, dst_dir_fd=fd)
                    os.fsync(fd)
            self._save_migration_record(marker, {**prepared, "state": "complete"})
            return target
        finally:
            # Remove only our unpublished staging tree, using validated parents.
            # An OS/process interruption may leave an inert sibling; never mount it.
            if self._entry_info(stage) is not None:
                for path in reversed(created_files):
                    with _directory(path.parent) as fd:
                        _regular(_lstat(path, fd), check_size=False)
                        os.unlink(path if fd is None else path.name, **({} if fd is None else {"dir_fd": fd}))
                for path in list(reversed(created_directories)) + [stage]:
                    with _directory(path.parent) as fd:
                        try:
                            os.rmdir(path if fd is None else path.name, **({} if fd is None else {"dir_fd": fd}))
                        except FileNotFoundError:
                            pass

    def _path(self, profile_id, path):
        parts = _parts(path)
        return self.directory(profile_id).joinpath(*parts)

    def read_file(self, profile_id, path):
        target = self._path(profile_id, path)
        with _open_file(target):
            return target

    def read_bytes(self, profile_id, path):
        """Read from the validated handle; never hand an unchecked path to ASGI."""
        with _open_file(self._path(profile_id, path)) as (stream, before):
            content = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
            _regular(after)
            if len(content) > MAX_FILE_BYTES:
                raise WorkspaceLimitError("File exceeds 10 MB workspace limit")
            if (len(content) != before.st_size or
                    (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise WorkspaceChangedError("Workspace file changed while reading; retry when the task stops")
            return content

    def list_files(self, profile_id):
        """Compatibility file list; use inventory() for completeness metadata.

        This may be a partial inventory. Never use it alone for quota checks.
        """
        return self.inventory(profile_id)["files"]

    def inventory(self, profile_id):
        """Bounded metadata scan. Limits truncate; unsafe I/O still fails closed."""
        with self._lock:
            base = self.directory(profile_id)
            rows = []
            entries = 0
            reasons = set()
            stop_scan = False
            pending = [(base, 0)]
            while pending and not stop_scan:
                directory, depth = pending.pop()
                with _directory(directory) as fd:
                    with os.scandir(directory if fd is None else fd) as scan:
                        for entry in scan:
                            entries += 1
                            if entries > MAX_ENTRIES:
                                reasons.add("entry_limit")
                                stop_scan = True
                                break
                            relative = (directory / entry.name).relative_to(base).as_posix()
                            try:
                                _parts(relative)
                            except WorkspaceLimitError:
                                reasons.add("depth_limit")
                                continue
                            except ValueError:
                                continue
                            # Windows DirEntry caches enumeration metadata with
                            # st_nlink=0; lstat obtains the actual link count.
                            info = _lstat(directory / entry.name, fd)
                            if _linked(info):
                                continue
                            if stat.S_ISDIR(info.st_mode):
                                if depth + 1 >= MAX_DEPTH:
                                    reasons.add("depth_limit")
                                    continue
                                pending.append((directory / entry.name, depth + 1))
                            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                                _regular(info, check_size=False)
                                if len(rows) >= MAX_FILES:
                                    reasons.add("file_count_limit")
                                    stop_scan = True
                                    break
                                rows.append({"path": relative, "size": info.st_size,
                                             "modified": info.st_mtime,
                                             "mime": mimetypes.guess_type(relative)[0] or "application/octet-stream",
                                             "oversized": info.st_size > MAX_FILE_BYTES,
                                             "previewable": info.st_size <= MAX_FILE_BYTES})
            return {"files": sorted(rows, key=lambda row: row["path"]),
                    "truncated": bool(reasons), "truncation_reasons": sorted(reasons)}

    def snapshot(self, profile_id, run_id):
        """Save a bounded subset; inspect snapshot_manifest for explicit omissions.

        Scan/size limits produce explicit incomplete manifests so a worker can
        still start to repair files. Security and changed-file failures raise.
        """
        if not isinstance(run_id, str) or not run_id or len(run_id) > 256:
            raise ValueError("Invalid workspace run identity")
        with self._lock:
            inventory = self.inventory(profile_id)
            rows = inventory["files"]
            contents = []
            omitted = []
            total = 0
            budget = min(MAX_WORKSPACE_BYTES, MAX_VERSION_BYTES)
            for row in rows:
                reason = ("file_too_large" if row["oversized"] else
                          "snapshot_byte_limit" if total + row["size"] > budget else None)
                if reason:
                    omitted.append({"path": row["path"], "size": row["size"], "reason": reason})
                    continue
                try:
                    data = self.read_bytes(profile_id, row["path"])
                except WorkspaceLimitError:
                    # A file grew beyond the body limit after inventory.
                    omitted.append({"path": row["path"], "size": row["size"], "reason": "file_too_large"})
                    continue
                if total + len(data) > budget:
                    omitted.append({"path": row["path"], "size": len(data), "reason": "snapshot_byte_limit"})
                    continue
                total += len(data)
                contents.append((row, data))
            manifest = {"complete": not omitted and not inventory["truncated"],
                        "saved_files": len(contents), "saved_bytes": total, "omitted": omitted,
                        "inventory_complete": not inventory["truncated"],
                        "truncation_reasons": inventory["truncation_reasons"]}
            snapshot_id = uuid.uuid4().hex
            with self._database() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT id FROM snapshots WHERE profile=? ORDER BY created DESC, rowid DESC",
                                      (profile_id,)).fetchall()
                for old in existing[MAX_SNAPSHOTS - 1:]:
                    db.execute("DELETE FROM snapshots WHERE id=?", (old["id"],))
                while True:
                    count, size = db.execute("SELECT count(*), coalesce(sum(size), 0) FROM snapshots").fetchone()
                    if count < MAX_GLOBAL_SNAPSHOTS and size + total <= MAX_VERSION_BYTES:
                        break
                    db.execute("DELETE FROM snapshots WHERE id=(SELECT id FROM snapshots ORDER BY created, rowid LIMIT 1)")
                db.execute("INSERT INTO snapshots (id,profile,run_id,created,size,manifest) VALUES (?, ?, ?, ?, ?, ?)",
                           (snapshot_id, profile_id, run_id, time.time(), total, json.dumps(manifest)))
                db.executemany("INSERT INTO files VALUES (?, ?, ?, ?, ?)",
                               [(snapshot_id, row["path"], row["modified"], len(data), data) for row, data in contents])
            return snapshot_id

    def snapshot_manifest(self, profile_id, snapshot_id):
        """Durable, profile-scoped completeness metadata; contains no file bodies."""
        validate_profile_id(profile_id)
        if not isinstance(snapshot_id, str) or not re.fullmatch(r"[0-9a-f]{32}", snapshot_id):
            raise ValueError("Invalid workspace version identity")
        with self._lock, self._database() as db:
            row = db.execute("""SELECT s.manifest,s.size,
                (SELECT count(*) FROM files f WHERE f.snapshot=s.id) AS count
                FROM snapshots s WHERE s.profile=? AND s.id=?""", (profile_id, snapshot_id)).fetchone()
            if row is None:
                raise FileNotFoundError("Workspace version not found")
            return {"complete": True, "saved_files": row["count"], "saved_bytes": row["size"],
                    "omitted": [], "inventory_complete": True, "truncation_reasons": [],
                    **json.loads(row["manifest"])}

    def versions(self, profile_id, path):
        validate_profile_id(profile_id)
        _parts(path)
        with self._lock, self._database() as db:
            rows = [dict(row) for row in db.execute("""
                SELECT s.id AS id, s.id AS version_id, s.run_id, s.created AS created_at,
                       s.created, f.path, f.size, f.modified, s.manifest
                FROM snapshots s JOIN files f ON f.snapshot=s.id
                WHERE s.profile=? AND f.path=? ORDER BY s.created DESC, s.rowid DESC LIMIT ?
            """, (profile_id, path, MAX_SNAPSHOTS))]
            for row in rows:
                manifest = json.loads(row.pop("manifest"))
                row["snapshot_complete"] = manifest.get("complete", True)
                row["snapshot_omitted_count"] = len(manifest.get("omitted", []))
                row["snapshot_inventory_complete"] = manifest.get("inventory_complete", True)
            return rows

    def read_version(self, profile_id, path, version_id):
        """Read a scoped version before retention can evict it during a mutation."""
        validate_profile_id(profile_id)
        _parts(path)
        if not isinstance(version_id, str) or not re.fullmatch(r"[0-9a-f]{32}", version_id):
            raise ValueError("Invalid workspace version identity")
        with self._lock:
            with self._database() as db:
                row = db.execute("""SELECT f.data FROM files f JOIN snapshots s ON s.id=f.snapshot
                                  WHERE s.profile=? AND s.id=? AND f.path=?""",
                                 (profile_id, version_id, path)).fetchone()
            if row is None:
                raise FileNotFoundError("Workspace version not found")
            if len(row["data"]) > MAX_FILE_BYTES:
                raise WorkspaceLimitError("File exceeds 10 MB workspace limit")
            return row["data"]

    def restore(self, profile_id, path, version_id):
        with self._lock:
            return self.write_file(profile_id, path, self.read_version(profile_id, path, version_id))

    def write_file(self, profile_id, path, content):
        """Atomic bounded upload/restore. Caller must exclude active model writers."""
        if len(content) > MAX_FILE_BYTES:
            raise WorkspaceLimitError("File exceeds 10 MB workspace limit")
        with self._lock:
            target = self._path(profile_id, path)
            inventory = self.inventory(profile_id)
            if inventory["truncated"]:
                raise WorkspaceLimitError("Workspace inventory is incomplete; repair it with a workspace task before uploading or restoring")
            rows = inventory["files"]
            remaining = [row for row in rows if row["path"] != path]
            if len(remaining) + 1 > MAX_FILES or sum(r["size"] for r in remaining) + len(content) > MAX_WORKSPACE_BYTES:
                raise WorkspaceLimitError("Workspace exceeds its budget")
            with _directory(target.parent, create=True) as fd:
                try:
                    _regular(_lstat(target, fd), check_size=False)
                except FileNotFoundError:
                    pass
                temp_name = ".upload-" + uuid.uuid4().hex
                temp_path = target.parent / temp_name
                temp_fd = os.open(temp_path if fd is None else temp_name,
                                  os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                                  0o600, **({} if fd is None else {"dir_fd": fd}))
                try:
                    with os.fdopen(temp_fd, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if fd is None:
                        os.replace(temp_path, target)
                    else:
                        os.replace(temp_name, target.name, src_dir_fd=fd, dst_dir_fd=fd)
                        os.fsync(fd)
                finally:
                    try:
                        os.unlink(temp_path if fd is None else temp_name, **({} if fd is None else {"dir_fd": fd}))
                    except FileNotFoundError:
                        pass
            return {"path": path, "size": len(content), "modified": target.stat().st_mtime,
                    "mime": mimetypes.guess_type(path)[0] or "application/octet-stream"}
