"""Safety/failure regressions for source acceptance (no application substitutes)."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/source_acceptance.py"


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "source acceptance helper is missing")
        spec = importlib.util.spec_from_file_location("source_acceptance", SCRIPT)
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)
        tmp = tempfile.TemporaryDirectory(prefix="source acceptance # % ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "package.json").write_text('{"version":"0.0.synthetic"}', encoding="utf-8")
        self.output = self.root / "output"

    def test_rejects_both_overlap_directions_without_writing_source(self):
        for output in (self.source, self.source / "nested", self.root):
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.helper.validate_paths(self.source, Path(sys.executable), output)
        self.assertEqual(list(self.source.iterdir()), [self.source / "package.json"])

    def test_resolves_overlap_through_directory_alias(self):
        alias = self.root / "alias"
        try:
            alias.symlink_to(self.source, target_is_directory=True)
        except OSError:
            self.skipTest("creating directory symlinks is unavailable")
        with self.assertRaises(ValueError):
            self.helper.validate_paths(self.source, Path(sys.executable), alias / "out")

    def test_rejects_reused_output_and_missing_interpreter(self):
        self.output.mkdir()
        (self.output / "acceptance.json").write_text("old evidence", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.helper.validate_paths(self.source, Path(sys.executable), self.output)
        with self.assertRaises(ValueError):
            self.helper.validate_paths(self.source, self.root / "missing python", self.root / "new")

    def test_preserves_venv_interpreter_symlink(self):
        interpreter = self.root / "venv-python"
        try:
            interpreter.symlink_to(sys.executable)
        except OSError:
            self.skipTest("creating interpreter symlinks is unavailable")
        _, validated, _ = self.helper.validate_paths(self.source, interpreter, self.output)
        self.assertEqual(validated, interpreter.absolute())
        self.assertNotEqual(validated, interpreter.resolve())

    def test_readonly_sqlite_handles_uri_metacharacters_without_creating_files(self):
        # '?' is legal on Unix, whereas '#', '%' and spaces also exercise Windows URIs.
        name = "legacy # % 雪" + ("?mode=rw" if os.name != "nt" else "") + ".db"
        path = self.root / name
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE messages (id INTEGER, role TEXT, content TEXT)")
            db.execute("INSERT INTO messages VALUES (41, 'user', 'retained body')")
            db.commit()
        rows, columns = self.helper.read_database(path)
        self.assertEqual(rows, [(41, "user", "retained body")])
        self.assertIn("content", columns)
        missing = self.root / "not created # %.db"
        with self.assertRaises(sqlite3.Error):
            self.helper.read_database(missing)
        self.assertFalse(missing.exists())

    def test_http_rejects_redirects_and_ignores_inherited_proxies(self):
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://example.invalid/credentials")
                self.end_headers()

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Redirect) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {"HTTP_PROXY": "http://invalid.invalid:9",
                                             "ALL_PROXY": "http://invalid.invalid:9", "NO_PROXY": ""}):
                    client = self.helper.LoopbackClient(server.server_port)
                    with self.assertRaisesRegex(RuntimeError, "302"):
                        client.request("/redirect")
                    for unsafe in ("//example.invalid/", "http://example.invalid/", "/\\evil", "/\r\nHost: evil"):
                        with self.subTest(path=unsafe), self.assertRaises(ValueError):
                            client.request(unsafe)
            finally:
                server.shutdown()
                thread.join(2)

    def test_startup_timeout_reaps_real_child_and_closes_log(self):
        self.output.mkdir()
        runner = self.helper.AppServer(self.source, Path(sys.executable), self.output, "hung")
        with patch.object(self.helper, "BOOTSTRAP", "import time; time.sleep(60)"), \
             patch.object(self.helper, "STARTUP_TIMEOUT", 0.3), \
             patch.object(self.helper, "STOP_TIMEOUT", 0.1):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "startup"):
                with runner:
                    self.fail("a child without uvicorn must never become ready")
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(runner.process.poll())
        self.assertTrue(runner.log.closed)
        # Windows may release shared file handles shortly after process termination.
        # Keep this bounded: a surviving child holding the log must still fail.
        log = self.output / "hung.log"
        deadline = time.monotonic() + 1
        while True:
            try:
                log.rename(self.output / "closed.log")
                break
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def test_startup_exit_reports_child_failure_and_keeps_diagnostics(self):
        self.output.mkdir()
        runner = self.helper.AppServer(self.source, Path(sys.executable), self.output, "crash")
        with patch.object(self.helper, "BOOTSTRAP", "print('synthetic crash', flush=True); raise SystemExit(23)"):
            with self.assertRaisesRegex(RuntimeError, "23"):
                with runner:
                    self.fail("exited process cannot be ready")
        self.assertEqual(runner.process.poll(), 23)
        self.assertIn("synthetic crash", (self.output / "crash.log").read_text(encoding="utf-8"))

    def test_child_guard_blocks_network_dns_and_processes_before_application_import(self):
        audit = self.root / "blocked.jsonl"
        code = self.helper.AUDIT_GUARD + "\n" + r'''
import subprocess
for action in (lambda: socket.create_connection(('192.0.2.1', 443), timeout=.1),
               lambda: socket.getaddrinfo('example.invalid', 443),
               lambda: socket.socket().bind(('0.0.0.0', 0)),
               lambda: subprocess.Popen([sys.executable, '-c', 'pass'])):
    try:
        action()
    except PermissionError:
        continue
    raise AssertionError('guard permitted forbidden activity')
'''
        result = subprocess.run([sys.executable, "-I", "-c",
                                 "import sys, socket, json\nfrom pathlib import Path\n"
                                 f"audit_path = Path({str(audit)!r})\nallowed_port = 1\n" + code],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line)["event"] for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(events), 4)
        self.assertIn("subprocess.Popen", events)

    def test_sanitized_environment_cannot_override_fixture_or_python_imports(self):
        with patch.dict(os.environ, {"ACCEPTANCE_LOCAL_API_KEY": "private",
                                     "GOOGLE_API_KEY": "private", "PYTHONPATH": "private",
                                     "SHIKIGAMI_APP_ROOT": "private", "HTTPS_PROXY": "private"}):
            env = self.helper.child_environment(self.source)
        self.assertNotIn("ACCEPTANCE_LOCAL_API_KEY", env)
        self.assertNotIn("GOOGLE_API_KEY", env)
        for name in ("PYTHONPATH", "SHIKIGAMI_APP_ROOT", "HTTPS_PROXY"):
            self.assertNotIn(name, env)

    def test_guard_allows_stdlib_platform_metadata_for_sdk_headers(self):
        audit = self.root / "platform.jsonl"
        code = ("import sys, socket, json\nfrom pathlib import Path\n"
                f"audit_path = Path({str(audit)!r})\n" + self.helper.AUDIT_GUARD
                + "\nimport platform\nprint(platform.platform())\n")
        result = subprocess.run([sys.executable, "-I", "-c", code],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(audit.exists(), audit.read_text() if audit.exists() else "")

    def test_failure_writes_machine_readable_synthetic_report(self):
        argv = ["--source", str(self.source), "--python", sys.executable, "--output", str(self.output)]
        with patch.object(self.helper, "run_acceptance", side_effect=RuntimeError("synthetic failure")), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.helper.main(argv), 1)
        report = json.loads((self.output / "acceptance.json").read_text(encoding="utf-8"))
        self.assertFalse(report["ok"])
        self.assertTrue(report["synthetic_only"])
        self.assertEqual(report["version"], "0.0.synthetic")
        self.assertIn("synthetic failure", report["error"])

    def test_legacy_fixture_is_raw_old_schema_and_detects_authored_data_loss(self):
        self.helper.write_fixture(self.source, 12345, legacy=True)
        db = self.source / "profiles" / self.helper.PROFILE_ID / "chat_records.db"
        rows, columns = self.helper.read_database(db)
        self.assertEqual(rows, self.helper.LEGACY_MESSAGES)
        self.assertNotIn("agent_run_id", columns)
        self.assertNotIn("generation_status", columns)
        card = json.loads((self.source / "profiles" / (self.helper.PROFILE_ID + ".json")).read_text(encoding="utf-8"))
        self.assertIn("content", card["user_portrait"])


if __name__ == "__main__":
    unittest.main()
