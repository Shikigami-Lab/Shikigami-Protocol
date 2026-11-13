"""Run real launchers in disposable trees; never install deps or start the app.

Windows exercises CMD; POSIX exercises Bash (including macOS system Bash).
Only minimal config modules are copied. All install commands and browser openers
are intercepted, and server.py is a harmless exit-code/working-directory probe.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"


class LaunchScriptsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shikigami launch ! & space ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        self.log = self.root / "calls.log"
        for name in ("init.bat", "init.sh", "launch.bat", "launch.sh"):
            # Preserve bytes: CRLF in a shipped .sh must fail the POSIX tests.
            shutil.copyfile(ROOT / name, self.root / name)
        helper = ROOT / "scripts/launch_browser.py"
        if helper.is_file():
            (self.root / "scripts").mkdir()
            shutil.copyfile(helper, self.root / "scripts/launch_browser.py")
        for name in ("src/config/app_config.py", "src/utils/paths.py"):
            dest = self.root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, dest)
        (self.root / "config").mkdir()
        (self.root / "config/app.yaml.example").write_text("port: 7788\n", encoding="utf-8")
        (self.root / ".env.example").write_text("SYNTHETIC=1\n", encoding="utf-8")
        (self.root / "requirements.txt").write_text("", encoding="utf-8")
        (self.root / "package.json").write_text('{"version":"0.12.0"}', encoding="utf-8")
        (self.root / "server.py").write_text(
            "import os, pathlib, sys\n"
            "with open(os.environ['LAUNCH_TEST_LOG'], 'a') as f:\n"
            "    f.write('server:' + str(pathlib.Path.cwd()) + '\\n')\n"
            "sys.exit(int(os.environ.get('SERVER_EXIT', '0')))\n", encoding="utf-8")
        # A real, disposable interpreter supports config parsing. sitecustomize
        # intercepts pip/venv before Python can install anything, even on failure.
        (self.bin / "sitecustomize.py").write_text(
            "import os, sys\n"
            "args = sys.orig_argv[1:]\n"
            "if '-c' in args and 'print(sys.executable)' in args[args.index('-c') + 1] and os.environ.get('PYTHON_PROBE_EXE'):\n"
            "    sys.executable = os.environ['PYTHON_PROBE_EXE']\n"
            "if '-m' in args and args[args.index('-m') + 1] in ('pip', 'venv'):\n"
            "    action = args[args.index('-m') + 1]\n"
            "    with open(os.environ['LAUNCH_TEST_LOG'], 'a') as f: f.write(action + '\\n')\n"
            "    os._exit(int(os.environ.get(action.upper() + '_EXIT', '0')))\n"
            "if '--open' in args or ('-c' in args and 'webbrowser' in args[args.index('-c') + 1]):\n"
            "    import time, webbrowser\n"
            "    time.sleep = lambda _: None\n"
            "    def opened(url):\n"
            "        with open(os.environ['LAUNCH_TEST_LOG'], 'a') as f: f.write('browser:' + url + '\\n')\n"
            "        return True\n"
            "    webbrowser.open = opened\n"
            "if os.environ.get('OLD_PYTHON'):\n"
            "    sys.version_info = tuple(int(x) for x in os.environ['OLD_PYTHON'].split('.'))\n",
            encoding="utf-8")
        venv.EnvBuilder(with_pip=False, system_site_packages=True).create(self.root / ".venv")
        self.python = self.root / (".venv/Scripts/python.exe" if WINDOWS else ".venv/bin/python")
        self.env = os.environ.copy()
        self.env.update(PATH=str(self.bin) + os.pathsep + self.env["PATH"],
                        PYTHONPATH=str(self.bin), LAUNCH_TEST_LOG=str(self.log),
                        PYTHONUTF8="1")
        self.command("node", "echo 0.12.0", "echo 0.12.0")
        self.command("npm", 'echo npm >> "%LAUNCH_TEST_LOG%"\nexit /b %NPM_EXIT%',
                     'echo npm >> "$LAUNCH_TEST_LOG"\nexit "${NPM_EXIT:-0}"')
        self.env["NPM_EXIT"] = "0"
        # The old scripts call pip directly; this is also intercepted safely.
        self.command("pip", 'echo pip >> "%LAUNCH_TEST_LOG%"\nexit /b %PIP_EXIT%',
                     'echo pip >> "$LAUNCH_TEST_LOG"\nexit "${PIP_EXIT:-0}"',
                     directory=self.python.parent)
        self.env["PIP_EXIT"] = "0"
        if WINDOWS:
            # CMD executes FOR /F command strings with delayed expansion disabled;
            # the root deliberately contains spaces and an exclamation mark.
            self.command("py", f'"{sys.executable}" %2 %3', "")
            self.command("python", f'"{sys.executable}" %*', "")
        else:
            for name in ("python3.12", "python3.13", "python3.11", "python3.10", "python3", "python"):
                self.command(name, "", 'exec "' + sys.executable + '" "$@"')
            self.command("sleep", "", "exit 0")
            for name in ("open", "xdg-open"):
                self.command(name, "", 'echo "browser:$1" >> "$LAUNCH_TEST_LOG"')

    def command(self, name, bat, shell, directory=None):
        directory = directory or self.bin
        path = directory / (name + ".cmd" if WINDOWS else name)
        path.write_text("@echo off\n" + bat + "\n" if WINDOWS else "#!/bin/sh\n" + shell + "\n",
                        encoding="utf-8")
        path.chmod(0o755)

    def run_script(self, name, **env):
        self.env.update(env)
        if WINDOWS:
            # /s removes the outer quote pair; keep a second pair around the
            # batch path so CMD receives metacharacters only inside quotes.
            cmd = os.environ.get("COMSPEC", "cmd.exe")
            args = f'"{cmd}" /d /s /c ""{self.root / (name + ".bat")}""'
        else:
            args = ["bash", str(self.root / (name + ".sh"))]
        return subprocess.run(args, cwd=self.root.parent, env=self.env, input="\n" * 10,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)

    def calls(self):
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def electron(self):
        path = self.root / ("node_modules/.bin/electron.cmd" if WINDOWS else "node_modules/.bin/electron")
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")

    def test_browser_uses_configured_port_and_preserves_server_exit(self):
        (self.root / "config/app.yaml").write_text("port: '8912' # custom\n", encoding="utf-8")
        result = self.run_script("launch", SERVER_EXIT="7")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn("http://127.0.0.1:8912", result.stdout)
        self.assertIn("server:" + str(self.root), self.calls())
        self.assertIn("browser:http://127.0.0.1:8912", self.calls())

    def test_browser_respects_explicit_lan_host(self):
        (self.root / "config/app.yaml").write_text("host: 192.0.2.10\nport: 8912\n", encoding="utf-8")
        result = self.run_script("launch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("http://192.0.2.10:8912", result.stdout)
        self.assertIn("browser:http://192.0.2.10:8912", self.calls())

    def test_browser_maps_ipv4_wildcard_to_loopback(self):
        (self.root / "config/app.yaml").write_text("host: 0.0.0.0\nport: 8912\n", encoding="utf-8")
        result = self.run_script("launch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("http://127.0.0.1:8912", result.stdout)

    def test_browser_formats_ipv6_and_maps_wildcard(self):
        for host, expected in [("::", "::1"), ("2001:db8::10", "2001:db8::10")]:
            with self.subTest(host=host):
                (self.root / "config/app.yaml").write_text(f"host: '{host}'\nport: 8912\n", encoding="utf-8")
                result = self.run_script("launch")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"http://[{expected}]:8912", result.stdout)

    def test_browser_rejects_host_with_shell_metacharacters(self):
        (self.root / "config/app.yaml").write_text('host: \'bad" & echo INJECTED & "\'\n', encoding="utf-8")
        result = self.run_script("launch")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("server:", self.calls())
        self.assertNotIn("INJECTED", result.stdout)

    def test_browser_uses_example_port_before_init(self):
        (self.root / "config/app.yaml.example").write_text("port: 8913\n", encoding="utf-8")
        result = self.run_script("launch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("http://127.0.0.1:8913", result.stdout)

    def test_browser_uses_default_without_config(self):
        (self.root / "config/app.yaml.example").unlink()
        result = self.run_script("launch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("http://127.0.0.1:7788", result.stdout)

    def test_browser_rejects_invalid_port_before_starting_server(self):
        for port in ("not-a-port", "0", "65536"):
            with self.subTest(port=port):
                (self.root / "config/app.yaml").write_text(f"port: {port}\n", encoding="utf-8")
                result = self.run_script("launch")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("server:", self.calls())

    def test_browser_can_use_system_python_without_venv(self):
        (self.root / ".venv").rename(self.root / "saved-venv")
        if WINDOWS:
            (self.bin / "python.cmd").unlink()
            self.env["PATH"] = str(self.bin) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + self.env["PATH"]
        result = self.run_script("launch", SERVER_EXIT="7")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn("server:", self.calls())

    def test_electron_failure_propagates(self):
        self.electron()
        result = self.run_script("launch", NPM_EXIT="9")
        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertNotIn("server:", self.calls())

    def test_init_npm_failure_is_not_success(self):
        result = self.run_script("init", NPM_EXIT="8")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Setup done", result.stdout)

    def test_init_pip_failure_stops_before_npm(self):
        result = self.run_script("init", PIP_EXIT="6")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("npm\n", self.calls())
        self.assertNotIn("Setup done", result.stdout)

    def test_init_keeps_existing_config_and_environment(self):
        (self.root / "config/app.yaml").write_text("port: 8914\n", encoding="utf-8")
        (self.root / ".env").write_text("KEEP=1\n", encoding="utf-8")
        marker = self.root / ".venv/keep.txt"
        marker.write_text("keep", encoding="utf-8")
        result = self.run_script("init")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Setup done", result.stdout)
        self.assertEqual((self.root / ".env").read_text(), "KEEP=1\n")
        self.assertEqual((self.root / "config/app.yaml").read_text(), "port: 8914\n")
        self.assertEqual(marker.read_text(), "keep")
        self.assertNotIn("venv\n", self.calls())

    def test_init_copies_first_run_templates(self):
        result = self.run_script("init")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / ".env").read_text(), "SYNTHETIC=1\n")
        self.assertEqual((self.root / "config/app.yaml").read_text(), "port: 7788\n")

    def test_init_never_deletes_broken_venv(self):
        self.python.unlink()
        marker = self.root / ".venv/keep.txt"
        marker.write_text("keep", encoding="utf-8")
        result = self.run_script("init")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(marker.is_file(), result.stdout + result.stderr)
        self.assertNotIn("venv\n", self.calls())

    def test_init_rejects_python_older_than_310(self):
        # Prevent Windows' common-install-path fallback from finding the host's
        # unrelated installation when all fake PATH candidates are rejected.
        for version in ("2.7", "3.9"):
            with self.subTest(version=version):
                result = self.run_script("init", OLD_PYTHON=version,
                                         LocalAppData=str(self.root / "no-install"),
                                         ProgramFiles=str(self.root / "no-install"))
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("pip\n", self.calls())

    def test_init_accepts_python_310_boundary(self):
        result = self.run_script("init", OLD_PYTHON="3.10")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(WINDOWS, "CMD interpreter path quoting")
    def test_init_handles_python_install_path_with_metacharacters(self):
        result = self.run_script("init", PYTHON_PROBE_EXE=str(self.python))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")

    def test_init_skips_an_unusable_preferred_python(self):
        if WINDOWS:
            self.command("py", 'if "%1" == "-3.12" exit /b 1\n' + f'"{sys.executable}" %2 %3', "")
        else:
            self.command("python3.12", "", 'OLD_PYTHON=3.9 exec "' + sys.executable + '" "$@"')
        result = self.run_script("init")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if not WINDOWS:
            self.assertIn(str(self.bin / "python3.13"), result.stdout)

    def test_init_venv_creation_failure_stops_before_install(self):
        # Move only this test's disposable environment; no recursive deletion.
        (self.root / ".venv").rename(self.root / "saved-venv")
        result = self.run_script("init", VENV_EXIT="5")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("venv\n", self.calls())
        self.assertNotIn("pip\n", self.calls())

    def test_init_skips_npm_when_electron_already_exists(self):
        self.electron()
        result = self.run_script("init", NPM_EXIT="8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("npm\n", self.calls())

    def test_launch_rejects_old_venv(self):
        result = self.run_script("launch", OLD_PYTHON="3.9")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("server:", self.calls())


if __name__ == "__main__":
    unittest.main()
