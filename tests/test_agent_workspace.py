"""Offline workspace acceptance/security tests; all files live in temp directories."""
import asyncio
import base64
import importlib
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx


def workspace_module():
    try:
        return importlib.import_module("src.agent.workspace")
    except ModuleNotFoundError as error:
        raise AssertionError("WorkspaceStore has not been implemented") from error


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-workspace-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.module = workspace_module()
        self.store = self.module.WorkspaceStore(self.root / "workspaces")

    def write(self, name="report.txt", content=b"before", profile="alpha"):
        path = self.store.directory(profile) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_restart_restore_and_profile_isolation(self):
        path = self.write("artifacts/report.txt")
        version = self.store.snapshot("alpha", "before-task")
        path.write_bytes(b"after")
        self.store.snapshot("alpha", "after-task")
        self.store = self.module.WorkspaceStore(self.root / "workspaces")
        self.assertEqual(self.store.read_file("alpha", "artifacts/report.txt"), path)
        rows = self.store.versions("alpha", "artifacts/report.txt")
        self.assertEqual({row["version_id"] for row in rows}, {version, rows[0]["version_id"]})
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.store.versions("beta", "artifacts/report.txt"), [])
        with self.assertRaises(FileNotFoundError):
            self.store.restore("beta", "artifacts/report.txt", version)
        self.store.restore("alpha", "artifacts/report.txt", version)
        self.assertEqual(path.read_bytes(), b"before")
        path.unlink()
        self.store.restore("alpha", "artifacts/report.txt", version)
        self.assertEqual(path.read_bytes(), b"before")
        self.assertEqual([row["path"] for row in self.store.list_files("alpha")], ["artifacts/report.txt"])
        self.assertEqual(self.store.list_files("beta"), [])
        self.assertEqual(list(self.store.directory("alpha").rglob("*.sqlite3")), [])

    def test_opaque_profile_directories_and_validation(self):
        self.assertNotEqual(self.store.directory("Alpha"), self.store.directory("alpha"))
        self.assertNotIn("角色", str(self.store.directory("角色")))
        for value in ("", ".", "..", "../alpha", "x/y", "x\\y", "C:x", "x\x00", "x" * 257):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.directory(value)

    def test_real_path_traversal_and_private_names(self):
        self.write()
        (self.root / "secret.txt").write_text("outside", encoding="utf-8")
        for name in ("../secret.txt", "/secret.txt", "C:/secret.txt", "a/../../secret.txt",
                     "a\\..\\secret.txt", "report.txt:stream", "a//b", "a/./b", "NUL.txt",
                     "report.txt.", "report.txt ", ".git/config", ".env", "private/key", "runtime_state.json"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.read_file("alpha", name)

    def test_symlink_directory_and_file_never_read_or_archived(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"secret")
        work = self.store.directory("alpha")
        try:
            (work / "linked").symlink_to(outside, target_is_directory=True)
            (work / "secret.txt").symlink_to(outside / "secret.txt")
        except OSError as error:
            self.skipTest(f"OS does not permit symlink creation: {error}")
        for name in ("linked/secret.txt", "secret.txt"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.read_file("alpha", name)
        self.assertEqual(self.store.list_files("alpha"), [])
        self.store.snapshot("alpha", "links")
        self.assertEqual(self.store.versions("alpha", "secret.txt"), [])

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_real_windows_junction_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"secret")
        link = self.store.directory("alpha") / "junction"
        # Both paths are fixed children of this test's temporary directory.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                       check=True, capture_output=True)
        with self.assertRaises(ValueError):
            self.store.read_file("alpha", "junction/secret.txt")
        self.assertEqual(self.store.list_files("alpha"), [])
        self.store.snapshot("alpha", "junction")
        self.assertEqual(self.store.versions("alpha", "junction/secret.txt"), [])

    def test_hardlinks_are_rejected_and_restore_does_not_modify_external_file(self):
        path = self.write()
        version = self.store.snapshot("alpha", "before")
        path.unlink()
        outside = self.root / "outside.txt"
        outside.write_bytes(b"outside")
        os.link(outside, path)
        with self.assertRaises(ValueError):
            self.store.read_file("alpha", "report.txt")
        with self.assertRaises(ValueError):
            self.store.restore("alpha", "report.txt", version)
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertEqual(self.store.list_files("alpha"), [])

    def test_limits_are_fail_closed_and_snapshots_are_retained_boundedly(self):
        path = self.write(content=b"12345")
        with patch.object(self.module, "MAX_FILE_BYTES", 4):
            with self.assertRaises(self.module.WorkspaceLimitError):
                self.store.read_file("alpha", "report.txt")
            version = self.store.snapshot("alpha", "too-big")
            self.assertFalse(self.store.snapshot_manifest("alpha", version)["complete"])
        self.assertEqual(self.store.versions("alpha", "report.txt"), [])
        with patch.object(self.module, "MAX_SNAPSHOTS", 2):
            ids = [self.store.snapshot("alpha", str(index)) for index in range(4)]
        self.assertEqual({r["version_id"] for r in self.store.versions("alpha", "report.txt")}, set(ids[-2:]))
        self.write("second.txt")
        with patch.object(self.module, "MAX_FILES", 1):
            self.assertEqual(len(self.store.list_files("alpha")), 1)
            self.assertTrue(self.store.inventory("alpha")["truncated"])
        with patch.object(self.module, "MAX_WORKSPACE_BYTES", 6):
            version = self.store.snapshot("alpha", "too-many-bytes")
            self.assertFalse(self.store.snapshot_manifest("alpha", version)["complete"])
        for index in range(4):
            (path.parent / f".hidden{index}").mkdir()
        with patch.object(self.module, "MAX_ENTRIES", 3):
            version = self.store.snapshot("alpha", "too-many-entries")
            self.assertFalse(self.store.snapshot_manifest("alpha", version)["inventory_complete"])

    def test_scan_limits_allow_partial_snapshot_but_never_partial_quota_checks(self):
        for index in range(6):
            self.write(f"file{index}.txt", b"x")
        prior = self.store.snapshot("alpha", "before")
        for limit, reason in (("MAX_FILES", "file_count_limit"), ("MAX_ENTRIES", "entry_limit")):
            with self.subTest(limit=limit), patch.object(self.module, limit, 3):
                inventory = self.store.inventory("alpha")
                self.assertTrue(inventory["truncated"])
                self.assertEqual(inventory["truncation_reasons"], [reason])
                self.assertLessEqual(len(inventory["files"]), 3)
                version = self.store.snapshot("alpha", "limited")
                manifest = self.store.snapshot_manifest("alpha", version)
                self.assertFalse(manifest["complete"])
                self.assertFalse(manifest["inventory_complete"])
                self.assertEqual(manifest["truncation_reasons"], [reason])
                self.assertEqual(manifest["omitted"], [])  # Unscanned count is unknown, never zero.
                with self.assertRaises(self.module.WorkspaceLimitError):
                    self.store.write_file("alpha", "new.txt", b"x")
                with self.assertRaises(self.module.WorkspaceLimitError):
                    self.store.restore("alpha", "file0.txt", prior)

    def test_depth_limit_reports_partial_inventory_and_preserves_shallow_files(self):
        self.write("shallow.txt", b"ok")
        self.write("deep/a/b/file.txt", b"deep")
        with patch.object(self.module, "MAX_DEPTH", 2):
            version = self.store.snapshot("alpha", "depth")
            manifest = self.store.snapshot_manifest("alpha", version)
            self.assertFalse(manifest["complete"])
            self.assertEqual(manifest["truncation_reasons"], ["depth_limit"])
            self.assertEqual(self.store.read_bytes("alpha", "shallow.txt"), b"ok")
            self.assertEqual(manifest["saved_files"], 1)

    def test_oversized_inventory_snapshot_and_repair_survive_restart(self):
        self.write("large.txt", b"12345")
        self.write("small.txt", b"ok")
        with patch.object(self.module, "MAX_FILE_BYTES", 4):
            rows = self.store.list_files("alpha")
            self.assertTrue(rows[0]["oversized"])
            self.assertFalse(rows[0]["previewable"])
            original = self.store.read_bytes
            with patch.object(self.store, "read_bytes", side_effect=original) as read:
                version = self.store.snapshot("alpha", "partial")
                self.assertEqual([call.args[1] for call in read.call_args_list], ["small.txt"])
            self.store = self.module.WorkspaceStore(self.root / "workspaces")
            manifest = self.store.snapshot_manifest("alpha", version)
            self.assertFalse(manifest["complete"])
            self.assertEqual(manifest["saved_files"], 1)
            self.assertEqual(manifest["omitted"], [{"path": "large.txt", "size": 5, "reason": "file_too_large"}])
            self.assertEqual(self.store.versions("alpha", "large.txt"), [])
            with self.assertRaises(FileNotFoundError):
                self.store.snapshot_manifest("beta", version)
            self.store.write_file("alpha", "large.txt", b"fix")
            repaired = self.store.snapshot("alpha", "repaired")
            self.assertTrue(self.store.snapshot_manifest("alpha", repaired)["complete"])

    def test_snapshot_byte_budget_skips_bodies_and_keeps_smaller_files(self):
        self.write("a.txt", b"1234")
        self.write("b.txt", b"1234")
        self.write("c.txt", b"x")
        with patch.object(self.module, "MAX_WORKSPACE_BYTES", 5):
            self.assertEqual(len(self.store.list_files("alpha")), 3)
            original = self.store.read_bytes
            with patch.object(self.store, "read_bytes", side_effect=original) as read:
                version = self.store.snapshot("alpha", "bounded")
                self.assertEqual([call.args[1] for call in read.call_args_list], ["a.txt", "c.txt"])
            manifest = self.store.snapshot_manifest("alpha", version)
            self.assertEqual(manifest["saved_bytes"], 5)
            self.assertEqual(manifest["omitted"], [{"path": "b.txt", "size": 4, "reason": "snapshot_byte_limit"}])
            self.assertFalse(self.store.versions("alpha", "a.txt")[0]["snapshot_complete"])

    def test_legacy_snapshot_schema_migrates_without_losing_restorable_data(self):
        path = self.write(content=b"old")
        version = self.store.snapshot("alpha", "legacy")
        # Reconstruct the old schema in this test's temporary database only.
        with self.store._database() as db:
            db.execute("ALTER TABLE snapshots DROP COLUMN manifest")
        self.store = self.module.WorkspaceStore(self.root / "workspaces")
        self.assertEqual(self.store.snapshot_manifest("alpha", version),
                         {"complete": True, "saved_files": 1, "saved_bytes": 3, "omitted": [],
                          "inventory_complete": True, "truncation_reasons": []})
        path.write_bytes(b"changed")
        self.store.restore("alpha", "report.txt", version)
        self.assertEqual(path.read_bytes(), b"old")

    def test_atomic_restore_failure_preserves_current_content(self):
        path = self.write()
        version = self.store.snapshot("alpha", "before")
        path.write_bytes(b"current")
        with patch.object(self.module.os, "replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.store.restore("alpha", "report.txt", version)
        self.assertEqual(path.read_bytes(), b"current")
        self.assertEqual([p.name for p in path.parent.iterdir()], ["report.txt"])

    def test_global_snapshot_budget_prunes_oldest_across_profiles(self):
        self.write(content=b"1234")
        self.write(content=b"5678", profile="beta")
        with patch.object(self.module, "MAX_VERSION_BYTES", 6):
            self.store.snapshot("alpha", "task-before")
            version = self.store.snapshot("beta", "task-after")
        self.assertEqual(self.store.versions("alpha", "report.txt"), [])
        self.assertEqual(self.store.versions("beta", "report.txt")[0]["version_id"], version)

    def test_changed_read_is_rejected_instead_of_serving_partial_content(self):
        self.write(content=b"before")
        original = self.module.os.fstat
        calls = 0

        def changed_stat(fd):
            nonlocal calls
            calls += 1
            value = original(fd)
            if calls == 2:
                return SimpleNamespace(st_mode=value.st_mode, st_nlink=value.st_nlink,
                                       st_size=value.st_size, st_file_attributes=0,
                                       st_dev=value.st_dev, st_ino=value.st_ino,
                                       st_mtime_ns=value.st_mtime_ns + 1,
                                       st_ctime_ns=value.st_ctime_ns + 1)
            return value

        with patch.object(self.module.os, "fstat", side_effect=changed_stat):
            with self.assertRaises(ValueError):
                self.store.read_bytes("alpha", "report.txt")

    def test_snapshot_skips_private_runtime_and_metadata_stays_outside_mount(self):
        self.write("notes.txt", b"public")
        self.write(".git/config", b"private")
        self.write("private/runtime.txt", b"private")
        self.write("runtime_state.json", b"private")
        version = self.store.snapshot("alpha", "run-id-before")
        self.assertEqual([r["path"] for r in self.store.list_files("alpha")], ["notes.txt"])
        self.assertEqual(self.store.versions("alpha", "notes.txt")[0]["version_id"], version)
        self.assertFalse((self.store.directory("alpha") / "versions.sqlite3").exists())


class WorkspaceAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-workspace-api-")
        self.addCleanup(self.tmp.cleanup)
        self.module = workspace_module()
        self.store = self.module.WorkspaceStore(Path(self.tmp.name) / "workspaces")
        self.runtime = SimpleNamespace(workspaces=self.store, _runs={}, _state_lock=threading.RLock())
        self.app = FastAPI()
        self.app.state.agent_runtime = self.runtime
        self.lookups = []

        def lookup(profile_id):
            self.lookups.append(profile_id)
            return SimpleNamespace(profile_id=profile_id) if profile_id in ("alpha", "beta") else None

        self.app.state.session_manager = SimpleNamespace(get_by_id=lookup)
        self.app.include_router(importlib.import_module("src.api.workspace").router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def request(self, method, endpoint, **kwargs):
        return await self.client.request(method, "/api/agent/workspace" + endpoint, **kwargs)

    async def upload(self, name="report.txt", content=b"hello", **kwargs):
        return await self.request("POST", "/upload", data={"profile_id": "alpha"},
                                  files={"file": (name, content)}, **kwargs)

    async def test_upload_list_preview_download_versions_restore(self):
        response = await self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["path"], "report.txt")
        version = self.store.snapshot("alpha", "before")
        self.assertEqual((await self.upload(content=b"changed")).status_code, 200)
        response = await self.request("GET", "/files", params={"profile_id": "alpha"})
        self.assertEqual(response.json()["files"][0]["size"], 7)
        response = await self.request("GET", "/versions", params={"profile_id": "alpha", "path": "report.txt"})
        self.assertIn(version, [row["version_id"] for row in response.json()["versions"]])
        self.assertEqual(response.json()["versions"][0]["id"], response.json()["versions"][0]["version_id"])
        self.assertIsInstance(response.json()["versions"][0]["created_at"], (int, float))
        response = await self.request("POST", "/restore", json={"profile_id": "alpha", "path": "report.txt", "version_id": version})
        self.assertEqual(response.status_code, 200, response.text)
        response = await self.request("GET", "/file", params={"profile_id": "alpha", "path": "report.txt"})
        self.assertEqual(response.content, b"hello")
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        response = await self.request("GET", "/file", params={"profile_id": "alpha", "path": "report.txt", "download": True})
        self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))

    async def test_two_uploads_and_restore_preserve_both_contents_without_runtime_snapshots(self):
        first = await self.upload(content=b"first")
        self.assertEqual(first.status_code, 200)
        versions = (await self.request("GET", "/versions", params={"profile_id": "alpha", "path": "report.txt"})).json()["versions"]
        self.assertEqual(len(versions), 1)
        first_id = versions[0]["version_id"]
        second = await self.upload(content=b"second")
        self.assertEqual(second.status_code, 200)
        versions = (await self.request("GET", "/versions", params={"profile_id": "alpha", "path": "report.txt"})).json()["versions"]
        self.assertIn(first_id, [row["version_id"] for row in versions])
        second_id = versions[0]["version_id"]
        restored = await self.request("POST", "/restore", json={"profile_id": "alpha", "path": "report.txt", "version_id": first_id})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self.store.read_bytes("alpha", "report.txt"), b"first")
        recovered = await self.request("POST", "/restore", json={"profile_id": "alpha", "path": "report.txt", "version_id": second_id})
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(self.store.read_bytes("alpha", "report.txt"), b"second")

    async def test_restore_reads_selected_version_before_retention_evicts_it(self):
        self.store.write_file("alpha", "report.txt", b"selected")
        with patch.object(self.module, "MAX_SNAPSHOTS", 2):
            selected = self.store.snapshot("alpha", "oldest")
            self.store.write_file("alpha", "report.txt", b"current")
            self.store.snapshot("alpha", "newest")
            response = await self.request("POST", "/restore", json={"profile_id": "alpha", "path": "report.txt", "version_id": selected})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.store.read_bytes("alpha", "report.txt"), b"selected")
            versions = self.store.versions("alpha", "report.txt")
            self.assertNotIn(selected, [row["version_id"] for row in versions])
            self.assertEqual(len(versions), 2)
            response = await self.request("POST", "/restore", json={"profile_id": "alpha", "path": "report.txt", "version_id": versions[1]["version_id"]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.store.read_bytes("alpha", "report.txt"), b"current")

    async def test_active_content_never_executes_and_raster_preview_is_explicit(self):
        for name in ("attack.html", "attack.svg", "attack.js", "attack.xml", "fake.png"):
            await self.upload(name, b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>')
            response = await self.request("GET", "/file", params={"profile_id": "alpha", "path": name})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.headers["content-type"].startswith(("text/html", "image/svg", "image/png")))
            self.assertIn("sandbox", response.headers["content-security-policy"])
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aE1cAAAAASUVORK5CYII=")
        await self.upload("pixel.png", png)
        response = await self.request("GET", "/file", params={"profile_id": "alpha", "path": "pixel.png"})
        self.assertEqual(response.headers["content-type"], "image/png")

    async def test_origin_profile_and_traversal_rejected_before_storage_access(self):
        response = await self.upload(headers={"origin": "https://evil.invalid"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.lookups, [])
        for profile, status in (("missing", 404), ("../alpha", 400)):
            response = await self.request("GET", "/files", params={"profile_id": profile})
            self.assertEqual(response.status_code, status)
        for path in ("../secret.txt", ".git/config", "a\\b.txt"):
            response = await self.upload(path)
            self.assertEqual(response.status_code, 400, response.text)
        response = await self.request("GET", "/file", params={"profile_id": "alpha", "path": "missing.txt"})
        self.assertEqual(response.status_code, 404)

    async def test_busy_profile_and_terminal_worker_block_mutations_only(self):
        await self.upload()
        version = self.store.snapshot("alpha", "before")
        task = asyncio.create_task(asyncio.Event().wait())
        try:
            for terminal in (False, True):
                self.runtime._runs["busy"] = SimpleNamespace(profile_id="alpha", terminal=terminal, task=task)
                self.assertEqual((await self.upload(content=b"bad")).status_code, 409)
                response = await self.request("POST", "/restore", json={"profile_id": "alpha", "path": "report.txt", "version_id": version})
                self.assertEqual(response.status_code, 409)
                response = await self.request("GET", "/files", params={"profile_id": "alpha"})
                self.assertEqual(response.status_code, 200)
            self.runtime._runs["busy"].profile_id = "beta"
            self.assertEqual((await self.upload()).status_code, 200)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_pending_container_cleanup_blocks_upload_restore_but_not_inventory(self):
        await self.upload()
        version = self.store.snapshot("alpha", "before")
        self.runtime.cleanup_pending = lambda profile: profile == "alpha"
        self.assertEqual((await self.upload(content=b"replacement")).status_code, 409)
        response = await self.request("POST", "/restore", json={"profile_id": "alpha", "path": "report.txt", "version_id": version})
        self.assertEqual(response.status_code, 409)
        response = await self.request("GET", "/files", params={"profile_id": "alpha"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.read_bytes("alpha", "report.txt"), b"hello")
        response = await self.request("POST", "/upload", data={"profile_id": "beta"}, files={"file": ("other.txt", b"ok")})
        self.assertEqual(response.status_code, 200)

    async def test_oversized_file_is_visible_unpreviewable_and_replaceable(self):
        await self.upload(content=b"12345")
        with patch.object(self.module, "MAX_FILE_BYTES", 4), patch.object(self.module, "MAX_WORKSPACE_BYTES", 4):
            response = await self.request("GET", "/files", params={"profile_id": "alpha"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["over_byte_limit"])
            self.assertFalse(response.json()["files"][0]["previewable"])
            for download in (False, True):
                response = await self.request("GET", "/file", params={"profile_id": "alpha", "path": "report.txt", "download": download})
                self.assertEqual(response.status_code, 413)
            self.assertEqual((await self.upload(content=b"fix")).status_code, 200)
            version = self.store.snapshot("alpha", "fixed")
            self.assertTrue(self.store.snapshot_manifest("alpha", version)["complete"])

    async def test_truncated_inventory_and_snapshot_manifest_are_explicit_and_profile_scoped(self):
        for index in range(4):
            await self.upload(f"file{index}.txt", b"x")
        with patch.object(self.module, "MAX_FILES", 2):
            version = self.store.snapshot("alpha", "limited")
            response = await self.request("GET", "/files", params={"profile_id": "alpha"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["truncated"])
            self.assertTrue(response.json()["total_bytes_is_lower_bound"])
            self.assertIsNone(response.json()["over_byte_limit"])
            response = await self.request("GET", "/snapshot", params={"profile_id": "alpha", "snapshot_id": version})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["complete"])
            self.assertEqual(response.json()["truncation_reasons"], ["file_count_limit"])
            response = await self.request("GET", "/snapshot", params={"profile_id": "beta", "snapshot_id": version})
            self.assertEqual(response.status_code, 404)
            self.assertEqual((await self.upload("new.txt", b"x")).status_code, 413)

    async def test_upload_and_restore_limits_and_cross_profile_version(self):
        await self.upload()
        version = self.store.snapshot("alpha", "before")
        with patch.object(self.module, "MAX_FILE_BYTES", 4):
            response = await self.upload(content=b"12345")
            self.assertEqual(response.status_code, 413)
            response = await self.request("POST", "/restore", json={"profile_id": "alpha", "path": "report.txt", "version_id": version})
            self.assertEqual(response.status_code, 413)
        self.assertEqual(self.store.read_file("alpha", "report.txt").read_bytes(), b"hello")
        response = await self.request("POST", "/restore", json={"profile_id": "beta", "path": "report.txt", "version_id": version})
        self.assertEqual(response.status_code, 404)

    async def test_oversized_stream_stops_before_spooling_entire_multipart(self):
        chunks = 0

        async def body():
            nonlocal chunks
            yield (b'--test\r\nContent-Disposition: form-data; name="profile_id"\r\n\r\nalpha\r\n'
                   b'--test\r\nContent-Disposition: form-data; name="file"; filename="big.txt"\r\n'
                   b'Content-Type: application/octet-stream\r\n\r\n')
            for _ in range(50):
                chunks += 1
                yield b"x" * 16384
            yield b"\r\n--test--\r\n"

        with patch.object(self.module, "MAX_FILE_BYTES", 4):
            response = await self.request("POST", "/upload", content=body(),
                                          headers={"content-type": "multipart/form-data; boundary=test"})
        self.assertEqual(response.status_code, 413)
        self.assertLess(chunks, 10, "Request was fully buffered before enforcing the upload budget")

    async def test_task_starting_during_upload_read_is_denied(self):
        from starlette.datastructures import UploadFile
        original = UploadFile.read

        async def start_task(file, size=-1):
            self.runtime._runs["new"] = SimpleNamespace(profile_id="alpha", terminal=False, task=None)
            return await original(file, size)

        with patch.object(UploadFile, "read", start_task):
            response = await self.upload()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.list_files("alpha"), [])

    async def test_mutation_holds_the_runtime_admission_lock_through_snapshots_and_write(self):
        original_write = self.store.write_file
        original_snapshot = self.store.snapshot
        lock_available = []

        def observe_lock():
            def check():
                acquired = self.runtime._state_lock.acquire(blocking=False)
                lock_available.append(acquired)
                if acquired:
                    self.runtime._state_lock.release()
            thread = threading.Thread(target=check)
            thread.start()
            thread.join(timeout=2)

        def write(*args):
            observe_lock()
            return original_write(*args)

        def snapshot(*args):
            observe_lock()
            return original_snapshot(*args)

        with patch.object(self.store, "write_file", side_effect=write), \
                patch.object(self.store, "snapshot", side_effect=snapshot):
            first = await self.upload()
            self.assertEqual(first.status_code, 200)
            self.assertEqual((await self.upload(content=b"second")).status_code, 200)
            response = await self.request("POST", "/restore", json={"profile_id": "alpha", "path": "report.txt", "version_id": first.json()["snapshot_id"]})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(lock_available, [False] * 8)

    async def test_api_cannot_read_or_replace_external_hardlink(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_bytes(b"outside-secret")
        os.link(outside, self.store.directory("alpha") / "secret.txt")
        response = await self.request("GET", "/file", params={"profile_id": "alpha", "path": "secret.txt"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(b"outside-secret", response.content)
        self.assertEqual((await self.upload("secret.txt", b"overwrite")).status_code, 400)
        self.assertEqual(outside.read_bytes(), b"outside-secret")

    async def test_invalid_multipart_and_extra_files_never_write(self):
        response = await self.request("POST", "/upload", files={"file": ("x.txt", b"x")})
        self.assertEqual(response.status_code, 422)
        response = await self.request("POST", "/upload", data={"profile_id": "alpha"},
                                      files=[("file", ("x.txt", b"x")), ("file", ("y.txt", b"y"))])
        self.assertEqual(response.status_code, 400)
        response = await self.request("POST", "/upload", content=b"broken", headers={"content-type": "multipart/form-data"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list_files("alpha"), [])


if __name__ == "__main__":
    unittest.main()
