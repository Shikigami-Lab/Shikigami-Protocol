"""Copy-forward migration uses temporary fixtures only; no runtime or model calls."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from src.agent import workspace as module


class WorkspaceMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workspace-migration-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.root = self.project / "data" / "agent" / "workspaces"
        self.profiles = self.project / "profiles"
        self.old = module.WorkspaceStore(self.root)

    def store(self, allowed=None):
        return module.WorkspaceStore(self.root, profiles_root=self.profiles,
                                     migration_allowed=allowed)

    def legacy(self, profile="alpha", content=b"original"):
        base = self.old.directory(profile)
        (base / "nested" / "empty").mkdir(parents=True)
        (base / "nested" / "report.txt").write_bytes(content)
        return base

    def marker(self, profile="alpha"):
        return self.profiles / profile / ".workspace_migration.json"

    def test_migration_preserves_original_file_modification_time(self):
        old = self.legacy()
        source = old / 'nested/report.txt'
        historical = 1_700_000_000_000_000_000
        os.utime(source, ns=(historical, historical))
        expected = source.stat().st_mtime_ns
        target = self.store().directory('alpha') / 'nested/report.txt'
        self.assertEqual(target.stat().st_mtime_ns, expected)
        self.assertEqual(source.stat().st_mtime_ns, expected)

    def test_new_and_legacy_profiles_are_isolated_and_host_files_stay_outside(self):
        old = self.legacy()
        host = self.profiles / "alpha"
        host.mkdir(parents=True)
        private = {"todos.json": b"todos", "index.json": b"index", "notes.txt": b"notes"}
        for name, data in private.items():
            (host / name).write_bytes(data)
        (self.profiles / "alpha.json").write_bytes(b"profile")
        store = self.store()
        work = store.directory("alpha")
        self.assertEqual(work, host / "workspace")
        self.assertEqual(store.read_bytes("alpha", "nested/report.txt"), b"original")
        self.assertTrue((work / "nested" / "empty").is_dir())
        self.assertEqual((old / "nested" / "report.txt").read_bytes(), b"original")
        self.assertTrue(self.marker().is_file())
        for name, data in private.items():
            self.assertFalse((work / name).exists())
            self.assertEqual((host / name).read_bytes(), data)
        self.assertFalse((work / "alpha.json").exists())
        self.assertFalse((work / ".workspace_migration.json").exists())
        self.assertFalse((work / "versions.sqlite3").exists())
        other = store.directory("beta")
        self.assertEqual(other, self.profiles / "beta" / "workspace")
        self.assertEqual(list(other.iterdir()), [])
        self.assertFalse((self.root / "files" / hashlib.sha256(b"beta").hexdigest()).exists())

    def test_marker_survives_restart_and_never_recopies_or_checks_live_gate(self):
        old = self.legacy()
        work = self.store().directory("alpha")
        (work / "nested" / "report.txt").write_bytes(b"new active data")
        (old / "nested" / "report.txt").write_bytes(b"stale legacy")
        def forbidden(_):
            self.fail("Completed migration must not consult the live-task gate")
        restarted = self.store(forbidden)
        self.assertEqual(restarted.directory("alpha"), work)
        self.assertEqual(restarted.read_bytes("alpha", "nested/report.txt"), b"new active data")

    def test_host_marker_is_not_subject_to_workspace_file_byte_limit(self):
        self.legacy(content=b"ok")
        self.store().directory("alpha")
        with patch.object(module, "MAX_FILE_BYTES", 2):
            self.assertEqual(self.store(lambda _: False).read_bytes("alpha", "nested/report.txt"), b"ok")

    def test_live_or_cleanup_pending_gate_blocks_migration_but_not_new_profiles(self):
        old = self.legacy()
        calls = []
        def denied(profile):
            calls.append(profile)
            return False
        with self.assertRaisesRegex(ValueError, "(?i)(active|running|cleanup|migration.*blocked)"):
            self.store(denied).directory("alpha")
        self.assertEqual(calls, ["alpha"])
        self.assertFalse(self.marker().exists())
        self.assertFalse((self.profiles / "alpha" / "workspace").exists())
        self.assertEqual((old / "nested/report.txt").read_bytes(), b"original")
        self.assertTrue(self.store(denied).directory("new").is_dir())

    def test_collision_preserves_both_trees_without_merging(self):
        old = self.legacy()
        target = self.profiles / "alpha" / "workspace"
        target.mkdir(parents=True)
        (target / "new.txt").write_bytes(b"new")
        with self.assertRaisesRegex(ValueError, "(?i)(collision|both|conflict)"):
            self.store().directory("alpha")
        self.assertEqual(list(target.iterdir()), [target / "new.txt"])
        self.assertEqual((target / "new.txt").read_bytes(), b"new")
        self.assertEqual((old / "nested/report.txt").read_bytes(), b"original")
        self.assertFalse(self.marker().exists())

    def test_empty_destination_can_be_replaced(self):
        self.legacy()
        (self.profiles / "alpha/workspace").mkdir(parents=True)
        self.assertEqual(self.store().read_bytes("alpha", "nested/report.txt"), b"original")

    def test_copy_failure_never_publishes_and_restart_retries(self):
        old = self.legacy()
        def fail_copy(fd):
            raise OSError("simulated copy failure")
        with patch.object(module.os, "fsync", side_effect=fail_copy):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.store().directory("alpha")
        self.assertFalse((self.profiles / "alpha/workspace").exists())
        self.assertFalse(self.marker().exists())
        self.assertEqual((old / "nested/report.txt").read_bytes(), b"original")
        self.assertEqual(self.store().read_bytes("alpha", "nested/report.txt"), b"original")

    def test_early_copy_failure_preserves_error_and_cleans_only_created_paths(self):
        old = self.legacy()
        (old / "a.txt").write_bytes(b"first")
        (old / "z/deep").mkdir(parents=True)
        with patch.object(module.os, "fsync", side_effect=OSError("early copy failure")):
            with self.assertRaisesRegex(OSError, "early copy failure"):
                self.store().directory("alpha")
        self.assertEqual(list(self.profiles.glob("alpha/.workspace-stage-*")), [])
        self.assertFalse((self.profiles / "alpha/workspace").exists())

    def test_orphaned_partial_stage_is_never_used_as_workspace(self):
        self.legacy()
        stage = self.profiles / "alpha/.workspace-stage-abandoned"
        stage.mkdir(parents=True)
        (stage / "partial.txt").write_bytes(b"incomplete copy")
        store = self.store()
        self.assertNotEqual(store.directory("alpha"), stage)
        self.assertEqual(store.read_bytes("alpha", "nested/report.txt"), b"original")
        self.assertFalse((store.directory("alpha") / "partial.txt").exists())

    def test_two_existing_roles_migrate_independently(self):
        self.legacy("alpha", b"alpha")
        self.legacy("beta", b"beta")
        store = self.store()
        self.assertEqual(store.read_bytes("alpha", "nested/report.txt"), b"alpha")
        self.assertEqual(store.read_bytes("beta", "nested/report.txt"), b"beta")
        store.write_file("alpha", "nested/report.txt", b"alpha new")
        self.assertEqual(store.read_bytes("beta", "nested/report.txt"), b"beta")
        self.assertNotEqual(store.directory("alpha"), store.directory("beta"))

    def test_empty_legacy_does_not_conflict_with_existing_destination(self):
        self.old.directory("alpha")
        target = self.profiles / "alpha/workspace"
        target.mkdir(parents=True)
        (target / "report.txt").write_bytes(b"existing new data")
        self.assertEqual(self.store().directory("alpha"), target)
        self.assertEqual((target / "report.txt").read_bytes(), b"existing new data")
        self.assertTrue(self.marker().is_file())

    @unittest.skipUnless(os.name == "nt", "Windows case aliases")
    def test_case_alias_cannot_select_another_roles_workspace(self):
        store = self.store()
        store.write_file("alpha", "file.txt", b"alpha only")
        with self.assertRaisesRegex(ValueError, "(?i)marker"):
            store.directory("Alpha")

    def test_interruption_after_publish_recovers_only_the_verified_copy(self):
        self.legacy()
        original = module.os.replace
        markers = 0
        def interrupt(source, destination, **kwargs):
            nonlocal markers
            if str(destination).endswith(".workspace_migration.json"):
                markers += 1
                if markers == 2:
                    raise OSError("interrupted final marker")
            return original(source, destination, **kwargs)
        with patch.object(module.os, "replace", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.store().directory("alpha")
        self.assertEqual(json.loads(self.marker().read_text())["state"], "prepared")
        self.assertEqual(self.store().read_bytes("alpha", "nested/report.txt"), b"original")
        self.assertEqual(json.loads(self.marker().read_text())["state"], "complete")

    def test_interrupted_prepared_marker_does_not_adopt_changed_destination(self):
        self.legacy()
        original = module.os.replace
        def interrupt(source, destination, **kwargs):
            if str(destination).endswith(".workspace_migration.json") and (self.profiles / "alpha/workspace").exists():
                raise OSError("interrupted final marker")
            return original(source, destination, **kwargs)
        with patch.object(module.os, "replace", side_effect=interrupt):
            with self.assertRaises(OSError):
                self.store().directory("alpha")
        target = self.profiles / "alpha/workspace/nested/report.txt"
        target.write_bytes(b"independent data")
        with self.assertRaisesRegex(ValueError, "(?i)collision"):
            self.store().directory("alpha")
        self.assertEqual(target.read_bytes(), b"independent data")

    def test_copy_corruption_and_source_changes_prevent_publication(self):
        old = self.legacy()
        (old / "later/deeper").mkdir(parents=True)
        for change in ("source", "added", "copy"):
            with self.subTest(change=change):
                original = module.os.fsync
                changed = False
                def mutate(fd):
                    nonlocal changed
                    original(fd)
                    if changed:
                        return
                    changed = True
                    if change == "source":
                        (old / "nested/report.txt").write_bytes(b"changed!")
                    elif change == "added":
                        (old / "added.txt").write_bytes(b"new file")
                    else:
                        stage_file = next(self.profiles.glob("alpha/.workspace-stage-*/nested/report.txt"))
                        stage_file.write_bytes(b"corrupt!")
                with patch.object(module.os, "fsync", side_effect=mutate):
                    with self.assertRaises(module.WorkspaceChangedError):
                        self.store().directory("alpha")
                self.assertFalse((self.profiles / "alpha/workspace").exists())
                self.assertFalse(self.marker().exists())
                (old / "nested/report.txt").write_bytes(b"original")
                (old / "added.txt").unlink(missing_ok=True)

    def test_failure_before_publish_retries_after_prepared_marker(self):
        self.legacy()
        with patch.object(module.os, "rename", side_effect=OSError("publish interrupted")):
            with self.assertRaisesRegex(OSError, "publish interrupted"):
                self.store().directory("alpha")
        self.assertFalse((self.profiles / "alpha/workspace").exists())
        self.assertEqual(json.loads(self.marker().read_text())["state"], "prepared")
        self.assertEqual(self.store().read_bytes("alpha", "nested/report.txt"), b"original")

    def test_malformed_or_linked_marker_fails_closed(self):
        self.legacy()
        self.marker().parent.mkdir(parents=True)
        self.marker().write_bytes(b"not a migration marker")
        with self.assertRaisesRegex(ValueError, "(?i)marker"):
            self.store().directory("alpha")
        self.marker().unlink()
        outside = self.project / "outside.json"
        outside.write_bytes(b"{}")
        os.link(outside, self.marker())
        with self.assertRaises(ValueError):
            self.store().directory("alpha")
        self.assertEqual(outside.read_bytes(), b"{}")

    def test_marker_never_exposes_profile_root_when_workspace_missing(self):
        work = self.store().directory("alpha")
        work.rmdir()
        (work.parent / "todos.json").write_bytes(b"private")
        with self.assertRaises(FileNotFoundError):
            self.store().directory("alpha")

    def test_old_versions_download_and_restore_into_new_files_only(self):
        old = self.legacy()
        version = self.old.snapshot("alpha", "before-relocation")
        (old / "nested/report.txt").write_bytes(b"changed")
        store = self.store()
        self.assertEqual(store.read_version("alpha", "nested/report.txt", version), b"original")
        self.assertEqual(store.versions("alpha", "nested/report.txt")[0]["version_id"], version)
        store.restore("alpha", "nested/report.txt", version)
        self.assertEqual(store.read_bytes("alpha", "nested/report.txt"), b"original")
        self.assertEqual((old / "nested/report.txt").read_bytes(), b"changed")
        with self.assertRaises(FileNotFoundError):
            store.read_version("beta", "nested/report.txt", version)

    def test_over_limit_legacy_is_preserved_without_publication(self):
        old = self.legacy(content=b"12345")
        (old / "second.txt").write_bytes(b"x")
        for limit, value in (("MAX_FILE_BYTES", 4), ("MAX_WORKSPACE_BYTES", 5),
                             ("MAX_FILES", 1), ("MAX_ENTRIES", 2), ("MAX_DEPTH", 1)):
            with self.subTest(limit=limit), patch.object(module, limit, value):
                with self.assertRaises(module.WorkspaceLimitError):
                    self.store().directory("alpha")
                self.assertFalse((self.profiles / "alpha/workspace").exists())
                self.assertFalse(self.marker().exists())
                self.assertEqual((old / "nested/report.txt").read_bytes(), b"12345")

    def test_profile_ids_are_names_never_host_paths(self):
        store = self.store()
        for value in ("", ".", "..", "../alpha", "x/y", "x\\y", "C:x", "C:\\x",
                      "alpha.", "alpha ", "CON", "NUL.txt", "a*", "a?", ".hidden", "a\x00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                store.directory(value)
        self.assertEqual(store.directory("角色_1"), self.profiles / "角色_1/workspace")

    def test_nested_hardlink_is_rejected(self):
        old = self.legacy()
        outside = self.project / "outside.txt"
        outside.write_bytes(b"secret")
        os.link(outside, old / "nested/linked.txt")
        with self.assertRaises(ValueError):
            self.store().directory("alpha")
        self.assertFalse((self.profiles / "alpha/workspace").exists())
        self.assertEqual(outside.read_bytes(), b"secret")

    def test_nested_symlink_is_rejected(self):
        old = self.legacy()
        outside = self.project / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"secret")
        try:
            (old / "nested/link").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable: {error}")
        with self.assertRaises((ValueError, OSError)):
            self.store().directory("alpha")
        self.assertFalse((self.profiles / "alpha/workspace").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_nested_junction_is_rejected(self):
        old = self.legacy()
        outside = self.project / "outside"
        outside.mkdir()
        subprocess.run(["cmd", "/c", "mklink", "/J", str(old / "nested/junction"), str(outside)],
                       stdin=subprocess.DEVNULL, check=True, capture_output=True)
        with self.assertRaises((ValueError, OSError)):
            self.store().directory("alpha")
        self.assertFalse((self.profiles / "alpha/workspace").exists())

    @unittest.skipUnless(os.name == "nt", "Windows ancestor junction regression")
    def test_profile_and_workspace_junctions_never_expose_host_data(self):
        self.legacy()
        outside = self.project / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"host only")
        for name in ("alpha", "alpha/workspace"):
            with self.subTest(name=name):
                link = self.profiles / name
                link.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                               stdin=subprocess.DEVNULL, check=True, capture_output=True)
                try:
                    with self.assertRaises((ValueError, OSError)):
                        self.store().directory("alpha")
                finally:
                    link.rmdir()
                self.assertEqual((outside / "secret.txt").read_bytes(), b"host only")
                self.assertEqual(list(outside.iterdir()), [outside / "secret.txt"])


if __name__ == "__main__":
    unittest.main()
