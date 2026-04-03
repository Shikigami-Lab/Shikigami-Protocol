#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
启动前安装助手：仅执行 pip（不 import 项目 src），由 Electron 在启动 server 前子进程调用。
环境变量 SHIKIGAMI_APP_ROOT = 应用根目录（打包为 resourcesPath，开发为项目根）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def _emit(obj: dict) -> None:
    sys.stdout.write("SETUP_EVENT:" + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _app_root() -> str:
    return (os.environ.get("SHIKIGAMI_APP_ROOT") or "").strip() or os.getcwd()


def _is_packaged_layout(root: str) -> bool:
    if sys.platform == "win32":
        return os.path.isfile(os.path.join(root, "server.exe"))
    return os.path.isfile(os.path.join(root, "server")) and not os.path.isfile(os.path.join(root, "server.py"))


def _pip_python(root: str) -> tuple[str, str]:
    """返回 (python 可执行路径, 'frozen' | 'venv')。"""
    if _is_packaged_layout(root):
        if sys.platform == "win32":
            embed = os.path.join(root, "python_embed", "python.exe")
            if os.path.isfile(embed):
                return embed, "frozen"
        import shutil

        p = shutil.which("python3") or shutil.which("python") or sys.executable
        return p, "frozen"
    win = sys.platform == "win32"
    sub = os.path.join(".venv", "Scripts" if win else "bin", "python.exe" if win else "python")
    venv_py = os.path.join(root, sub)
    if os.path.isfile(venv_py):
        return venv_py, "venv"
    return sys.executable, "venv"


def cmd_pip_install(spec: dict) -> int:
    root = _app_root()
    packages = spec.get("packages") or []
    target = (spec.get("target") or "").strip()
    index_url = (spec.get("index_url") or "").strip()
    if not packages:
        _emit({"type": "pip", "phase": "error", "target": target, "error": "no packages"})
        return 1
    py, mode = _pip_python(root)
    if mode == "frozen":
        target_dir = os.path.join(root, "user_packages")
        os.makedirs(target_dir, exist_ok=True)
        cmd = [py, "-m", "pip", "install", "--upgrade", "--target", target_dir] + list(packages)
        if index_url:
            cmd += ["--index-url", index_url]
    else:
        cmd = [py, "-m", "pip", "install", "--upgrade"] + list(packages)
        if index_url:
            cmd += ["--index-url", index_url]
    _emit({"type": "pip", "phase": "running", "target": target, "message": "pip install…"})
    tail: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=root,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        if proc.stdout:
            for line in proc.stdout:
                line = line.rstrip()
                tail = (tail + [line])[-40:]
                _emit({"type": "pip", "phase": "running", "target": target, "message": line[-240:]})
        code = proc.wait(timeout=3600)
    except Exception as exc:
        _emit({"type": "pip", "phase": "error", "target": target, "error": str(exc)[:500]})
        return 1
    ok = code == 0
    err_tail = "\n".join(tail)[-800:] if not ok else None
    _emit(
        {
            "type": "pip",
            "phase": "success" if ok else "error",
            "target": target,
            "message": "完成" if ok else "失败",
            "error": err_tail,
        }
    )
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) < 2:
        return 1
    op = sys.argv[1]
    if op == "pip-install":
        raw = sys.argv[2] if len(sys.argv) > 2 else "{}"
        try:
            spec = json.loads(raw)
        except json.JSONDecodeError:
            _emit({"type": "pip", "phase": "error", "error": "invalid json"})
            return 1
        return cmd_pip_install(spec)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
