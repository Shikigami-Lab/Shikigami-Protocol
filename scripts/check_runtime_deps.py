"""
Runtime dependency sanity check for CI builds.

This script is intended to be run with the CI Python (before PyInstaller)
to ensure that all core stdlib and third-party modules required by the
public Shikigami Protocol server are installed and importable.

Exit code:
  0  -> all required modules are importable
  1  -> at least one required module is missing or failed to import
"""

from __future__ import annotations

import importlib
import sys
from typing import Iterable, List, Tuple


# Stdlib modules that should always be available in the runtime used by
# PyInstaller. This list is intentionally conservative and focused on
# modules that are either already used in the codebase or are common
# building blocks for future features.
REQUIRED_STDLIB: List[str] = [
    "asyncio",
    "concurrent.futures",
    "datetime",
    "json",
    "logging",
    "os",
    "pathlib",
    "random",
    "re",
    "shutil",
    "string",
    "subprocess",
    "time",
    "timeit",
    "traceback",
    "typing",
]


# Third-party modules that are required for the public server to run
# (and are listed in requirements.txt). Optional heavy components
# (Qwen3-TTS, Kokoro, etc.) are intentionally excluded here and are
# validated via their own onboarding flows.
REQUIRED_THIRDPARTY: List[str] = [
    "fastapi",
    "uvicorn",
    "pydantic",
    "httpx",
    "openai",
    "yaml",         # PyYAML
    "dotenv",       # python-dotenv
    "edge_tts",
    "websockets",
    "ruamel",       # ruamel.yaml and friends
    "soundfile",
]


def _check_imports(modules: Iterable[str], label: str) -> Tuple[bool, List[Tuple[str, str]]]:
    ok = True
    missing: List[Tuple[str, str]] = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as e:
            ok = False
            missing.append((name, repr(e)))
    if not ok:
        print(f"[{label}] missing or failed imports:", file=sys.stderr)
        for name, err in missing:
            print(f"  - {name}: {err}", file=sys.stderr)
    return ok, missing


def main() -> int:
    ok_std, _ = _check_imports(REQUIRED_STDLIB, "stdlib")
    ok_third, _ = _check_imports(REQUIRED_THIRDPARTY, "thirdparty")
    if ok_std and ok_third:
        print("[check_runtime_deps] All required modules are importable.")
        return 0
    print("[check_runtime_deps] Missing modules detected. Failing build.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

