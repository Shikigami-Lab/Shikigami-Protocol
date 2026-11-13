"""Offline release boundaries; all payloads and repositories are disposable."""
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts/leak_check.py"


class DockerPrivacyTests(unittest.TestCase):
    def context_files(self):
        # Keep the context policy deliberately simple: deny everything, then
        # admit literal files. No directory or wildcard exceptions can leak a
        # newly created local file beneath an otherwise public directory.
        rules = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
        self.assertEqual(rules[0], "**", "Docker context must deny unknown local files")
        for rule in rules[1:]:
            self.assertTrue(rule.startswith("!"), rule)
            self.assertFalse(any(c in rule[1:] for c in "*?[]\\"), rule)
            self.assertFalse(rule.endswith("/"), rule)
            self.assertTrue((ROOT / rule[1:]).is_file(), rule)
        return {rule[1:] for rule in rules[1:]}

    def test_context_only_admits_public_build_inputs(self):
        admitted = self.context_files()
        forbidden = {
            "config/app.yaml", "config/sessions_meta.json", "config/ui_prefs.json",
            "config/custom-local.yaml", "config/prompts/local-prompt.yaml",
            "src/local-debug.py", "static/local-session.json", "static/avatars/user.png",
            "profiles/personal.json", "profiles/example_luna/history.jsonl",
            ".env", ".env.production", "AGENTS.md", "CLAUDE.md", "docs/AGENTS.md",
            "docs/superpowers/specs/local.md", "docs/local-notes.md", ".git/config",
            "README.draft.md", "README_zh.review.html", "README.review.html",
            "strength-conditioning-audit/training-log.md", "local-audit/result.json",
            "data/reminders.sqlite3", "data/agent/workspaces/versions.sqlite3",
            "sessions/session.json", "startup_progress.json", "test-results/output.txt",
        }
        self.assertFalse(admitted & forbidden)
        self.assertTrue({"server.py", "package.json", "requirements.txt", "requirements-ai.txt",
                         "config/app.yaml.example", "config/model_bundles.yaml",
                         "profiles/example_luna.json", "profiles/example_mochi_en.json"} <= admitted)
        # Catch missing build inputs when adding public source/assets later.
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                                 capture_output=True, check=True).stdout.decode("utf-8").split("\0")
        for relative in tracked:
            if (relative.startswith(("src/", "static/", "assets/", "config/", "docs/", "profiles/", "integrations/"))
                    and not relative.startswith("integrations/deepseek-harness/tests/")):
                self.assertIn(relative, admitted)

    def test_compose_preserves_data_and_existing_mounts(self):
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        mounts = compose["services"]["shikigami"]["volumes"]
        for mount in ("./data:/app/data", "./profiles:/app/profiles", "./models:/app/models",
                      "./Voices:/app/Voices", "./config:/app/config", "./.env:/app/.env:ro"):
            self.assertIn(mount, mounts)


class LeakCheckerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.assertTrue(CHECKER.is_file(), "npm leak-check must have a tracked implementation")

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout

    def repo(self):
        self.git("init", "-q")
        self.git("config", "user.name", "Release Fixture")
        self.git("config", "user.email", "release@example.invalid")
        self.git("config", "commit.gpgsign", "false")

    def check(self):
        result = subprocess.run([sys.executable, str(CHECKER), "--root", str(self.root)],
                                capture_output=True, text=True, timeout=20)
        return result.returncode, result.stdout + result.stderr

    def test_tracked_secret_and_runtime_data_report_without_payloads(self):
        self.repo()
        payload = "sk-" + secrets.token_hex(24)
        self.write("settings.txt", "# synthetic test payload, not a usable credential\nAPI_KEY=" + payload + "\n")
        self.write("data/reminders.sqlite3", "synthetic reminder content")
        self.git("add", ".")
        code, output = self.check()
        self.assertEqual(code, 1, output)
        self.assertIn("settings.txt:2:credential", output)
        self.assertIn("data/reminders.sqlite3:1:runtime-data", output)
        self.assertNotIn(payload, output)
        self.assertNotIn("synthetic reminder content", output)

    def test_untracked_local_files_are_not_read_in_git_mode(self):
        self.repo()
        self.write("source.py", "print('fixture')\n")
        self.git("add", "source.py")
        self.write(".env", "API_KEY=" + "sk-" + secrets.token_hex(24))
        code, output = self.check()
        self.assertEqual(code, 0, output)

    def test_unpushed_commit_body_is_checked_without_printing_subject_or_secret(self):
        self.repo()
        self.write("source.py", "print('fixture')\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Public fixture baseline")
        self.git("update-ref", "refs/remotes/origin/dev", "HEAD")
        payload = "ghp_" + secrets.token_hex(20)
        self.git("commit", "--allow-empty", "-qm", "Fixture subject must not be echoed\n\n" + payload)
        sha = self.git("rev-parse", "--short", "HEAD").strip()
        code, output = self.check()
        self.assertEqual(code, 1, output)
        self.assertIn("commit:" + sha + ":3:credential", output)
        self.assertNotIn(payload, output)
        self.assertNotIn("Fixture subject must not be echoed", output)
        self.git("update-ref", "refs/remotes/origin/dev", "HEAD")
        self.assertEqual(self.check()[0], 0)

    def test_source_distribution_audits_files_and_explicitly_skips_commits(self):
        self.write("config/app.yaml.example", "api_key: ''\n")
        self.write("source.py", "print('fixture')\n")
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("Commit audit skipped", output)
        self.assertIn("no .git", output)
        payload = "sk-" + secrets.token_hex(24)
        self.write("source.py", "api_key = '" + payload + "'\n")
        code, output = self.check()
        self.assertEqual(code, 1, output)
        self.assertIn("source.py:1:credential", output)
        self.assertNotIn(payload, output)

    def test_synthetic_fixture_literals_are_allowed_but_tests_are_still_scanned(self):
        self.repo()
        self.write("tests/fixture.py", "\n".join([
            'API_KEY = "sk-test-' + 'a' * 40 + '"',
            'token = "synthetic-fixture-token-for-offline-testing"',
            'api_key = "your-api-key-here"',
            'api_key = os.environ.get("MODEL_API_KEY", "")',
            'api_key = "${MODEL_API_KEY}"',
            'api_key = "<replace-with-your-key>"',
            'TOKEN = secrets.token_hex(24)',
            'api_key = resolve_embedding_api_key(self._config)',
            'secret = self._key.encode() if self._key else b""',
            'token = memory.fact_action_revision(current)',
            'body.api_key = this.presetForm.api_key.trim();',
            'api_key: this.embeddingForm.api_key || "",',
            'logging.warning("password=unrelated-marker")',
            r'api_key = "fake\nINJECTED=bad"',
        ]))
        self.git("add", ".")
        code, output = self.check()
        self.assertEqual(code, 0, output)
        payload = "sk-" + secrets.token_hex(24)
        self.write("tests/fixture.py", 'api_key = "' + payload + '"\n')
        code, output = self.check()
        self.assertEqual(code, 1, output)
        self.assertNotIn(payload, output)

    def test_generic_secret_assignment_and_private_key_are_redacted(self):
        payload = secrets.token_hex(24)
        self.write("settings.json", json.dumps({"api_key": payload}, indent=2))
        self.write("key.pem", "-----BEGIN " + "PRIVATE KEY-----\n" + payload + "\n")
        code, output = self.check()
        self.assertEqual(code, 1, output)
        self.assertIn("settings.json:2:credential", output)
        self.assertIn("key.pem:1:private-key", output)
        self.assertNotIn(payload, output)

    def test_npm_entrypoint_works_in_fresh_source_distribution(self):
        target = self.root / "scripts/leak_check.py"
        target.parent.mkdir()
        shutil.copyfile(CHECKER, target)
        package = json.loads((ROOT / "package.json").read_text())
        self.write("package.json", json.dumps({"name": "release-fixture", "scripts": {
            "leak-check": package["scripts"]["leak-check"]}}))
        npm = shutil.which("npm.cmd") or shutil.which("npm")
        if not npm:
            self.skipTest("npm not installed")
        result = subprocess.run([npm, "run", "leak-check", "--silent"], cwd=self.root,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Commit audit skipped", result.stdout)


if __name__ == "__main__":
    unittest.main()
