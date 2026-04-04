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


def _write_stdout_utf8_line(line: str) -> None:
    """管道输出固定 UTF-8，避免 Windows 控制台代码页导致 Electron 侧中文乱码。"""
    try:
        sys.stdout.buffer.write(line.encode("utf-8"))
        sys.stdout.buffer.flush()
    except (AttributeError, OSError, BrokenPipeError, ValueError):
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except Exception:
            pass


def _emit(obj: dict) -> None:
    _write_stdout_utf8_line("SETUP_EVENT:" + json.dumps(obj, ensure_ascii=False) + "\n")


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
            # Do NOT add --extra-index-url https://pypi.org/simple here:
            # PyPI publishes CPU-only torch wheels with higher version numbers than the
            # CUDA wheels on download.pytorch.org. pip's resolver picks the highest version
            # across all indices, so adding PyPI causes it to install CPU torch even when
            # the user explicitly requested a CUDA index-url.
            # download.pytorch.org/whl/cuXXX is self-contained for torch/torchvision/torchaudio.
    else:
        cmd = [py, "-m", "pip", "install", "--upgrade"] + list(packages)
        if index_url:
            cmd += ["--index-url", index_url]
            # Same reason: no --extra-index-url for pytorch CUDA installs.
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
    if not ok and not (err_tail and err_tail.strip()):
        err_tail = f"pip exited with code {code}"
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


def cmd_pip_uninstall(spec: dict) -> int:
    """Uninstall packages without importing any project code (avoids DLL lock on Windows)."""
    root = _app_root()
    packages = spec.get("packages") or []
    if not packages:
        _emit({"type": "pip", "phase": "error", "error": "no packages"})
        return 1

    py, mode = _pip_python(root)
    names = [p.split("[")[0].replace("-", "_") for p in packages]
    errors: list = []

    _emit({"type": "pip", "phase": "running", "message": "pip uninstall…"})

    if mode == "frozen":
        # In frozen layout packages live in user_packages/ — pip uninstall doesn't know about
        # --target dirs, so remove the directories/dist-info directly first.
        target_dir = os.path.join(root, "user_packages")
        if os.path.isdir(target_dir):
            for entry in os.listdir(target_dir):
                el = entry.lower().replace("-", "_")
                for nb in names:
                    nb_low = nb.lower()
                    if el == nb_low or el.startswith(nb_low + "-") or el.startswith(nb_low + "_") or el.startswith(nb_low + "."):
                        full = os.path.join(target_dir, entry)
                        try:
                            import shutil
                            if os.path.isdir(full):
                                shutil.rmtree(full)
                            else:
                                os.remove(full)
                            _emit({"type": "pip", "phase": "running", "message": f"removed {entry}"})
                        except Exception as exc:
                            errors.append(str(exc))
        # Also run pip uninstall as best-effort cleanup of any metadata
        cmd = [py, "-m", "pip", "uninstall", "-y"] + names
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=root)
        except Exception:
            pass
    else:
        # Source / venv layout: remove top-level site-packages dirs first (handles broken dist-info)
        try:
            import importlib.util as _ilu
            sp = None
            for p in packages:
                spec_obj = _ilu.find_spec(p.split("[")[0].replace("-", "_"))
                if spec_obj and spec_obj.origin:
                    candidate = os.path.dirname(spec_obj.origin)
                    # go up to site-packages
                    sp = os.path.dirname(candidate)
                    break
            if sp and os.path.isdir(sp):
                import shutil
                for entry in os.listdir(sp):
                    el = entry.lower().replace("-", "_")
                    for nb in names:
                        nb_low = nb.lower()
                        if el == nb_low or el.startswith(nb_low + "-") or el.startswith(nb_low + "_") or el.startswith(nb_low + "."):
                            full = os.path.join(sp, entry)
                            try:
                                if os.path.isdir(full):
                                    shutil.rmtree(full)
                                else:
                                    os.remove(full)
                                _emit({"type": "pip", "phase": "running", "message": f"removed {entry}"})
                            except Exception as exc:
                                errors.append(str(exc))
        except Exception:
            pass
        cmd = [py, "-m", "pip", "uninstall", "-y"] + names
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
                    _emit({"type": "pip", "phase": "running", "message": line.rstrip()[-240:]})
            code = proc.wait(timeout=300)
            if code != 0:
                errors.append(f"pip uninstall exited with code {code}")
        except Exception as exc:
            errors.append(str(exc))

    ok = len(errors) == 0
    result = {"ok": ok, "errors": errors}
    _write_stdout_utf8_line("SETUP_RESULT:" + json.dumps(result, ensure_ascii=False) + "\n")
    _emit({
        "type": "pip",
        "phase": "success" if ok else "error",
        "message": "完成" if ok else "失败",
        "error": "\n".join(errors)[:800] if errors else None,
    })
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
    if op == "pip-uninstall":
        raw = sys.argv[2] if len(sys.argv) > 2 else "{}"
        try:
            spec = json.loads(raw)
        except json.JSONDecodeError:
            _emit({"type": "pip", "phase": "error", "error": "invalid json"})
            return 1
        return cmd_pip_uninstall(spec)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
