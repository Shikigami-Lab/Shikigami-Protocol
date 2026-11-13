"""Offline checks for CI orchestration, isolation, and bounded diagnostics."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/run_package_acceptance.py"


class PackageAcceptanceRunnerTests(unittest.TestCase):
    def load_runner(self):
        self.assertTrue(SCRIPT.is_file(), "cross-platform package acceptance runner is missing")
        spec = importlib.util.spec_from_file_location("package_acceptance_runner", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_clean_external_venv_installs_extracted_requirements_before_acceptance(self):
        runner = self.load_runner()
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            package = temporary / "source-package"
            extracted = package / "extracted"
            extracted.mkdir(parents=True)
            (extracted / "requirements.txt").write_text("", encoding="utf-8")
            (package / "source.zip").write_bytes(b"synthetic archive")
            (package / "manifest.json").write_text("{}", encoding="utf-8")
            report = temporary / "source-package-acceptance"
            report.mkdir()
            (report / "acceptance.json").write_text("{}", encoding="utf-8")
            with patch.object(runner, "run_logged") as command:
                runner.run_acceptance(ROOT, temporary)
            calls = command.call_args_list
            self.assertEqual(len(calls), 4)
            build, venv, install, accept = [call.args[0] for call in calls]
            self.assertEqual(build, [sys.executable, str(ROOT / ".github/scripts/source_package.py"),
                                     "--output", str(package)])
            self.assertEqual(venv[:3], [sys.executable, "-m", "venv"])
            venv_root = Path(venv[3])
            self.assertEqual(venv_root.parent, temporary)
            self.assertFalse(venv_root.is_relative_to(extracted))
            self.assertEqual(list(venv_root.iterdir()), [])
            python = venv_root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            self.assertEqual(install, [str(python), "-m", "pip", "install",
                                       "--disable-pip-version-check", "-r",
                                       str(extracted / "requirements.txt")])
            self.assertEqual(accept, [sys.executable, str(ROOT / ".github/scripts/source_acceptance.py"),
                                     "--source", str(extracted), "--python", str(python),
                                     "--output", str(temporary / "source-package-acceptance")])
            self.assertEqual(calls[2].kwargs["cwd"], extracted)

    def test_each_run_uses_a_new_virtual_environment(self):
        runner = self.load_runner()
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            extracted = temporary / "source-package/extracted"
            extracted.mkdir(parents=True)
            (extracted / "requirements.txt").write_text("", encoding="utf-8")
            (extracted.parent / "source.zip").write_bytes(b"synthetic archive")
            (extracted.parent / "manifest.json").write_text("{}", encoding="utf-8")
            report = temporary / "source-package-acceptance"
            report.mkdir()
            (report / "acceptance.json").write_text("{}", encoding="utf-8")
            with patch.object(runner, "run_logged") as command:
                runner.run_acceptance(ROOT, temporary)
                runner.run_acceptance(ROOT, temporary)
            self.assertNotEqual(command.call_args_list[1].args[0][3],
                                command.call_args_list[5].args[0][3])

    def test_subprocess_failure_blocks_all_later_stages(self):
        runner = self.load_runner()
        for failed_stage in range(4):
            with self.subTest(stage=failed_stage), tempfile.TemporaryDirectory() as directory:
                temporary = Path(directory)
                extracted = temporary / "source-package/extracted"
                extracted.mkdir(parents=True)
                (extracted / "requirements.txt").write_text("", encoding="utf-8")
                (extracted.parent / "source.zip").write_bytes(b"synthetic archive")
                (extracted.parent / "manifest.json").write_text("{}", encoding="utf-8")
                failure = subprocess.CalledProcessError(7, ["synthetic-failure"])
                with patch.object(runner, "run_logged", side_effect=[None] * failed_stage + [failure]) as command:
                    with self.assertRaises(subprocess.CalledProcessError):
                        runner.run_acceptance(ROOT, temporary)
                self.assertEqual(command.call_count, failed_stage + 1)

    def test_logs_are_bounded_and_failures_propagate(self):
        runner = self.load_runner()
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "logs/synthetic.log"
            script = "import sys; sys.stdout.write('x' * 400000 + 'TAIL'); sys.stdout.flush(); sys.exit(9)"
            with self.assertRaises(subprocess.CalledProcessError) as error:
                runner.run_logged([sys.executable, "-c", script], log, cwd=Path(directory))
            self.assertEqual(error.exception.returncode, 9)
            self.assertLessEqual(log.stat().st_size, runner.LOG_LIMIT)
            self.assertTrue(log.read_bytes().endswith(b"TAIL"))

    def test_missing_package_requirements_blocks_installation(self):
        runner = self.load_runner()
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "source-package"
            package.mkdir()
            (package / "source.zip").write_bytes(b"synthetic archive")
            (package / "manifest.json").write_text("{}", encoding="utf-8")
            with patch.object(runner, "run_logged") as command:
                with self.assertRaisesRegex(FileNotFoundError, "requirements.txt"):
                    runner.run_acceptance(ROOT, Path(directory))
            self.assertEqual(command.call_count, 1)

    def test_missing_required_delivery_file_cannot_pass_the_gate(self):
        runner = self.load_runner()
        for missing in ("source.zip", "manifest.json", "acceptance.json"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                temporary = Path(directory)
                package = temporary / "source-package"
                extracted = package / "extracted"
                extracted.mkdir(parents=True)
                (extracted / "requirements.txt").write_text("", encoding="utf-8")
                report = temporary / "source-package-acceptance"
                report.mkdir()
                for name, parent in (("source.zip", package), ("manifest.json", package),
                                     ("acceptance.json", report)):
                    if name != missing:
                        (parent / name).write_text("synthetic", encoding="utf-8")
                with patch.object(runner, "run_logged"):
                    with self.assertRaises(FileNotFoundError):
                        runner.run_acceptance(ROOT, temporary)


if __name__ == "__main__":
    unittest.main()
