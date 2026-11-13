"""Offline safety regressions for the real-Electron source acceptance driver."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


SCRIPTS = Path(__file__).resolve().parents[1] / ".github/scripts"


class ElectronSmokeTests(unittest.TestCase):
    def setUp(self):
        script = SCRIPTS / "electron_smoke.py"
        self.assertTrue(script.is_file(), "desktop acceptance driver is missing")
        spec = importlib.util.spec_from_file_location("electron_smoke", script)
        self.smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.smoke)
        temporary = tempfile.TemporaryDirectory(prefix="electron smoke # % ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_fixture_uses_main_compatible_yaml_and_disables_background_work(self):
        runtime = self.root / "runtime"
        self.smoke.write_desktop_fixture(runtime, 51234, 51235)
        raw = (runtime / "config/app.yaml").read_text(encoding="utf-8")
        self.assertEqual(re.search(r"^port:\s*(\d+)", raw, re.M).group(1), "51235")
        self.assertEqual(re.search(r"^host:\s*([^\s]+)", raw, re.M).group(1), "127.0.0.1")
        config = yaml.safe_load(raw)
        for name in ("memory", "reflection", "ase", "agent", "stt", "topic_discovery"):
            self.assertFalse(config[name]["enabled"], name)
        for name in ("emotion", "affinity", "energy"):
            self.assertFalse(config["engines"][name]["enabled"])
        preset = config["llm_presets"][config["default_llm"]]
        self.assertEqual(preset["base_url"], "http://127.0.0.1:51234/v1")
        self.assertFalse(json.loads((runtime / "config/ui_prefs.json").read_text())["show_startup_launcher"])

    def test_environment_replaces_host_injections_with_only_the_test_guard(self):
        inherited = {"PATH": "host-path", "NODE_OPTIONS": "--require private.js",
                     "ELECTRON_RUN_AS_NODE": "1", "PYTHONPATH": "private", "PYTHONHOME": "private",
                     "HTTPS_PROXY": "private", "MODEL_API_KEY": "secret", "GITHUB_TOKEN": "secret",
                     "SHIKIGAMI_MODELS_ROOT": "private", "DISPLAY": ":42"}
        with patch.dict(os.environ, inherited, clear=True):
            env = self.smoke.desktop_environment(self.root, Path(sys.executable), self.root / "guard")
        for key in ("NODE_OPTIONS", "ELECTRON_RUN_AS_NODE", "PYTHONHOME", "HTTPS_PROXY",
                    "MODEL_API_KEY", "GITHUB_TOKEN", "SHIKIGAMI_MODELS_ROOT"):
            self.assertNotIn(key, env)
        self.assertEqual(env["PYTHONPATH"], str(self.root / "guard"))
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(Path(sys.executable).parent))
        self.assertEqual(env["DISPLAY"], ":42")

    def test_desktop_copy_does_not_modify_extraction_or_copy_node_modules(self):
        source = self.root / "extracted"
        source.mkdir()
        (source / "main.js").write_text("real entry", encoding="utf-8")
        (source / "node_modules").mkdir()
        (source / "node_modules/private").write_text("must not copy")
        runtime = self.root / "runtime"
        self.smoke.copy_runtime(source, runtime)
        self.assertEqual((runtime / "main.js").read_text(), "real entry")
        self.assertFalse((runtime / "node_modules").exists())
        self.assertFalse((source / "config").exists())

    def test_root_sandbox_exception_is_explicit_and_linux_only(self):
        for platform in ("win32", "darwin", "linux"):
            command = self.smoke.electron_command(self.root, self.root / "wrapper.cjs", platform=platform)
            self.assertNotIn("--no-sandbox", command)
        with patch.object(os, "geteuid", return_value=0, create=True):
            command = self.smoke.electron_command(self.root, self.root / "wrapper.cjs",
                                                  platform="linux", allow_no_sandbox=True)
        self.assertIn("--no-sandbox", command)
        with self.assertRaises(ValueError):
            self.smoke.electron_command(self.root, self.root / "wrapper.cjs",
                                        platform="darwin", allow_no_sandbox=True)

    def test_timeout_is_bounded_and_captures_output(self):
        log = self.root / "timeout.log"
        with self.assertRaises(subprocess.TimeoutExpired):
            self.smoke.run_bounded([sys.executable, "-c",
                                    "import time; print('synthetic timeout', flush=True); time.sleep(30)"],
                                   log, cwd=self.root, env=os.environ.copy(), timeout=0.5)
        self.assertIn("synthetic timeout", log.read_text())

    def test_bad_or_missing_renderer_evidence_cannot_pass(self):
        for data in ({}, {"ok": True}, {"ok": False, "error": "fixture error"}):
            with self.subTest(data=data):
                (self.root / "renderer.json").write_text(json.dumps(data))
                with self.assertRaises(RuntimeError):
                    self.smoke.read_renderer_evidence(self.root, 51235)

    def test_hosted_linux_sandbox_only_elevates_the_installed_temporary_helper(self):
        runtime = self.root / "runtime"
        helper = runtime / "node_modules/electron/dist/chrome-sandbox"
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"fixture helper")
        with patch.object(self.smoke.sys, "platform", "linux"), \
                patch.object(os, "geteuid", return_value=1001, create=True), \
                patch.object(self.smoke, "run_bounded") as run:
            self.smoke.prepare_sandbox(runtime, self.root, {"GITHUB_ACTIONS": "true"})
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["sudo", "-n", "chown", "root:root", str(helper)],
            ["sudo", "-n", "chmod", "4755", str(helper)],
        ])

    def test_sandbox_privilege_is_not_requested_outside_hosted_nonroot_linux(self):
        for platform, uid, env in (("win32", 1001, {"GITHUB_ACTIONS": "true"}),
                                   ("darwin", 1001, {"GITHUB_ACTIONS": "true"}),
                                   ("linux", 0, {}), ("linux", 1001, {})):
            with self.subTest(platform=platform, uid=uid, env=env), \
                    patch.object(self.smoke.sys, "platform", platform), \
                    patch.object(os, "geteuid", return_value=uid, create=True), \
                    patch.object(self.smoke, "run_bounded") as run:
                self.smoke.prepare_sandbox(self.root / "runtime", self.root, env)
                run.assert_not_called()

    def test_backend_guard_rejects_external_network_without_starting_app(self):
        guard = self.root / "guard"
        self.smoke.write_guard(guard, self.root / "network.log")
        env = self.smoke.desktop_environment(self.root, Path(sys.executable), guard)
        result = subprocess.run([sys.executable, "-c", "import socket; socket.create_connection(('192.0.2.1', 80))"],
                                cwd=self.root, env=env, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Acceptance permits loopback", result.stderr)
        self.assertIn("socket.", (self.root / "network.log").read_text())

    def test_clean_python_identity_accepts_windows_venv_launcher_but_rejects_unrelated_process(self):
        python = Path(sys.executable).absolute()
        loaded = {"pid": 12346, "parent_pid": 12345, "python": str(python), "prefix": sys.prefix}
        self.smoke.validate_python_evidence(loaded, python, 12345, sys.prefix)
        self.smoke.validate_python_evidence(loaded, python, 12346, sys.prefix)
        with self.assertRaises(RuntimeError):
            self.smoke.validate_python_evidence(loaded, python, 12345, str(self.root / "wrong-prefix"))
        with self.assertRaises(RuntimeError):
            self.smoke.validate_python_evidence(loaded, python, 54321, sys.prefix)
        loaded["python"] = str(self.root / "other-venv/python")
        with self.assertRaises(RuntimeError):
            self.smoke.validate_python_evidence(loaded, python, 12345, sys.prefix)

    def test_posix_python3_alias_preserves_the_supplied_virtual_environment(self):
        binary = self.root / "venv/bin"
        binary.mkdir(parents=True)
        try:
            (binary / "python").symlink_to(sys.executable)
            (binary / "python3").symlink_to(sys.executable)
        except OSError:
            self.skipTest("creating interpreter symlinks is unavailable")
        loaded = {"pid": 12345, "parent_pid": 1, "python": str(binary / "python3"),
                  "prefix": str(binary.parent)}
        self.smoke.validate_python_evidence(loaded, binary / "python", 12345, str(binary.parent))
        with self.assertRaises(RuntimeError):
            self.smoke.validate_python_evidence(loaded, binary / "python", 12345, sys.base_prefix)


if __name__ == "__main__":
    unittest.main()
