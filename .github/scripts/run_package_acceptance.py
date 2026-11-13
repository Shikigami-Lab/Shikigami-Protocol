"""Run the package builder and acceptance probe without shell-specific syntax."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


LOG_LIMIT = 256 * 1024


def run_logged(command, log, *, cwd):
    """Keep a bounded output tail while preserving the child's failure status."""
    log.parent.mkdir(parents=True, exist_ok=True)
    tail = bytearray()
    try:
        with subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT) as process:
            while chunk := process.stdout.read(8192):
                tail.extend(chunk)
                del tail[:-LOG_LIMIT]
            returncode = process.wait()
    finally:
        log.write_bytes(tail)
    if returncode:
        raise subprocess.CalledProcessError(returncode, command)


def run_acceptance(root, runner_temp):
    """Use runner-local outputs and a fresh venv outside the extracted source."""
    root = Path(root).resolve()
    runner_temp = Path(runner_temp).resolve()
    package = runner_temp / "source-package"
    extracted = package / "extracted"
    report = runner_temp / "source-package-acceptance"
    logs = report / "logs"
    scripts = root / ".github/scripts"

    run_logged([sys.executable, str(scripts / "source_package.py"),
                "--output", str(package)], logs / "runner-build.log", cwd=root)
    requirements = extracted / "requirements.txt"
    for required in (package / "source.zip", package / "manifest.json", requirements):
        if not required.is_file():
            raise FileNotFoundError(f"Package output missing: {required}")

    venv = Path(tempfile.mkdtemp(prefix="source-package-venv-", dir=runner_temp))
    run_logged([sys.executable, "-m", "venv", str(venv)], logs / "runner-venv.log", cwd=root)
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    run_logged([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                "-r", str(requirements)], logs / "runner-install.log", cwd=extracted)
    run_logged([sys.executable, str(scripts / "source_acceptance.py"),
                "--source", str(extracted), "--python", str(python), "--output", str(report)],
               logs / "runner-probe.log", cwd=root)
    if not (report / "acceptance.json").is_file():
        raise FileNotFoundError(f"Acceptance report missing: {report / 'acceptance.json'}")


if __name__ == "__main__":
    run_acceptance(Path(__file__).resolve().parents[2], Path(os.environ["RUNNER_TEMP"]))
