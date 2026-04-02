"""
Build script for the PyInstaller server binary.

Replaces the inline pyinstaller command in release.yml. Generates a
comprehensive --hidden-import list from sys.stdlib_module_names so that
stdlib modules loaded dynamically at runtime (e.g. timeit, pickletools)
are never missing from the packaged binary.

Usage (called by CI):
    python scripts/build_binary.py
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys


def _all_stdlib_hidden_imports() -> list[str]:
    """Return --hidden-import flags for every importable stdlib module."""
    result = []
    for name in sorted(sys.stdlib_module_names):
        if name.startswith("_"):
            continue
        try:
            if importlib.util.find_spec(name) is not None:
                result.extend(["--hidden-import", name])
        except (ModuleNotFoundError, ValueError):
            pass
    return result


def main() -> int:
    sep = ";" if sys.platform == "win32" else ":"

    collect_all_packages = [
        "uvicorn",
        "fastapi",
        "starlette",
        "pydantic",
        "edge_tts",
        "httpx",
        "jinja2",
        "anyio",
        "sniffio",
        "click",
        "websockets",
        "yaml",
        "ruamel",
        "dotenv",
        "openai",
        "pydantic_core",
    ]

    hidden_imports_fixed = [
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ]

    cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "server.py"]

    for pkg in collect_all_packages:
        cmd += ["--collect-all", pkg]

    for hi in hidden_imports_fixed:
        cmd += ["--hidden-import", hi]

    # Bulk-add every importable stdlib module so dynamic imports never fail.
    cmd += _all_stdlib_hidden_imports()

    cmd += ["--add-data", f"package.json{sep}."]

    print("Running:", " ".join(cmd[:6]), "... [+stdlib hidden imports]")
    result = subprocess.run(cmd)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
