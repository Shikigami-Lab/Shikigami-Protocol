"""Accept the source desktop with real Electron, Python and a loopback-only model."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

# These helpers are scripts, not an installed application package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from source_acceptance import (AUDIT_GUARD, FixtureModel, PROFILE_ID, REPLY,
                               child_environment, free_port, validate_paths,
                               write_fixture, write_json)

CHECKS = ["real-main-spawned-backend", "vue-rendered", "settings-opened", "chat-reply-rendered"]
LOG_LIMIT = 256 * 1024


def copy_runtime(source, runtime):
    shutil.copytree(source, runtime, ignore=shutil.ignore_patterns("node_modules", ".venv", "__pycache__"))


def write_desktop_fixture(runtime, model_port, backend_port):
    if backend_port in (7788, 8000) or backend_port == model_port:
        raise ValueError("Desktop acceptance requires a separate ephemeral backend port")
    write_fixture(runtime, model_port)
    path = runtime / "config/app.yaml"
    config = json.loads(path.read_text(encoding="utf-8"))
    config.update(port=backend_port, topic_discovery={"enabled": False},
                  engines={name: {"enabled": False} for name in ("emotion", "affinity", "energy")})
    config["memory"].update(day_summary_enabled=False, embedding={"enabled": False})
    # JSON values are valid YAML. Top-level host/port MUST use simple YAML lines:
    # source main.js deliberately uses a small regex parser rather than PyYAML.
    path.write_text("".join(key + ": " + (str(value) if key in ("host", "port") else json.dumps(value)) + "\n"
                            for key, value in config.items()), encoding="utf-8")
    write_json(runtime / "config/ui_prefs.json", {"show_startup_launcher": False, "locale": "en"})


def write_guard(directory, audit):
    directory.mkdir(parents=True, exist_ok=True)
    # A trusted, generated sitecustomize observes the unmodified server.py startup.
    # No inherited Python path or application monkeypatch is used.
    script = ("import sys, json, os\nfrom pathlib import Path\n"
              f"audit_path = Path({str(audit)!r})\n" + AUDIT_GUARD +
              f"Path({str(directory / 'loaded.json')!r}).write_text(json.dumps("
              "{'pid': os.getpid(), 'parent_pid': os.getppid(), 'python': sys.executable, 'prefix': sys.prefix}), encoding='utf-8')\n")
    (directory / "sitecustomize.py").write_text(script, encoding="utf-8")


def desktop_environment(runtime, python, guard):
    env = child_environment(runtime)
    denied = {"ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "NODE_PATH", "SHIKIGAMI_MODELS_ROOT",
              "ELECTRON_EXTRA_LAUNCH_ARGS", "ELECTRON_OVERRIDE_DIST_PATH"}
    env = {key: value for key, value in env.items() if key.upper() not in denied}
    # Source main.js's normal python/python3 fallback resolves this exact venv.
    path_key = next((key for key in env if key.upper() == "PATH"), "PATH")
    env[path_key] = str(python.parent) + os.pathsep + env.get(path_key, "")
    env["PYTHONPATH"] = str(guard)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def kill_tree(process):
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=15)


def run_bounded(command, log, *, cwd, env, timeout):
    log.parent.mkdir(parents=True, exist_ok=True)
    kwargs = ({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32"
              else {"start_new_session": True})
    try:
        with log.open("wb") as output:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output,
                                       stderr=subprocess.STDOUT, **kwargs)
            try:
                code = process.wait(timeout=timeout)
            except BaseException:
                kill_tree(process)
                raise
        if code:
            raise subprocess.CalledProcessError(code, command)
    finally:
        if log.exists() and log.stat().st_size > LOG_LIMIT:
            with log.open("rb") as output:
                output.seek(-LOG_LIMIT, os.SEEK_END)
                tail = output.read()
            log.write_bytes(tail)


def electron_command(runtime, wrapper, *, platform=sys.platform, allow_no_sandbox=False):
    binary = {"win32": "electron.exe", "darwin": "Electron.app/Contents/MacOS/Electron",
              "linux": "electron"}[platform]
    command = [str(runtime / "node_modules/electron/dist" / binary)]
    if allow_no_sandbox:
        if platform != "linux" or os.geteuid() != 0:
            raise ValueError("--allow-no-sandbox is only for explicit local Linux root testing")
        command.append("--no-sandbox")
    return command + [str(wrapper)]


def prepare_sandbox(runtime, output, env):
    """Keep Electron's sandbox enabled on hosted Linux, without global changes."""
    if sys.platform != "linux" or env.get("GITHUB_ACTIONS") != "true" or os.geteuid() == 0:
        return "system-default"
    helper = runtime / "node_modules/electron/dist/chrome-sandbox"
    if not helper.is_file() or helper.is_symlink() or not helper.resolve().is_relative_to(runtime.resolve()):
        raise RuntimeError("Installed temporary Electron sandbox helper is missing or unsafe")
    for operation, value in (("chown", "root:root"), ("chmod", "4755")):
        run_bounded(["sudo", "-n", operation, value, str(helper)], output / f"logs/sandbox-{operation}.log",
                    cwd=runtime, env=env, timeout=15)
    return "temporary-helper-root-4755"


def read_renderer_evidence(output, port):
    data = json.loads((output / "renderer.json").read_text(encoding="utf-8"))
    if (data.get("ok") is not True or data.get("checks") != CHECKS
            or data.get("url") != f"http://127.0.0.1:{port}/"
            or not isinstance(data.get("backend_pid"), int)
            or not data.get("electron") or not data.get("arch")):
        raise RuntimeError("Incomplete real-Electron renderer evidence: " + str(data.get("error", "missing checks")))
    for name in ("settings.png", "chat.png"):
        image = output / name
        if not image.is_file() or not image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("Desktop screenshot missing: " + name)
    return data


def validate_python_evidence(loaded, python, backend_pid, expected_prefix):
    actual = Path(loaded["python"]).absolute()
    # On Windows a venv python.exe is a launcher with a real Python child;
    # on POSIX main.js uses bin/python3, an alias of the supplied bin/python.
    if (backend_pid not in (loaded["pid"], loaded.get("parent_pid"))
            or Path(loaded["prefix"]).resolve() != Path(expected_prefix).resolve()
            or actual.parent != python.parent or not actual.is_file()
            or not actual.samefile(python)):
        raise RuntimeError("Electron did not launch the guarded clean Python interpreter")


def port_open(port):
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def cleanup_backend(output, port):
    """Fallback for an Electron crash; kill only its recorded synthetic child PID."""
    deadline = time.monotonic() + 5
    while port_open(port) and time.monotonic() < deadline:
        time.sleep(0.1)
    if port_open(port):
        path = output / "backend.json"
        if path.exists():
            pid = json.loads(path.read_text(encoding="utf-8"))["pid"]
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=15,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 5
        while port_open(port) and time.monotonic() < deadline:
            time.sleep(0.1)
        if port_open(port):
            raise RuntimeError("Synthetic backend did not stop")


def run_smoke(source, python, output, *, allow_no_sandbox=False):
    source, python, output = validate_paths(source, python, output)
    output.mkdir(parents=True, exist_ok=True)
    result = {"ok": False, "synthetic_only": True, "platform": sys.platform,
              "machine": platform.machine(), "version": json.loads((source / "package.json").read_text())["version"]}
    FixtureModel.calls = 0
    try:
        with ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel) as model:
            thread = threading.Thread(target=model.serve_forever, daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory(prefix="desktop-runtime-", dir=output) as temporary:
                    runtime = Path(temporary) / "source"
                    copy_runtime(source, runtime)
                    port = free_port()
                    write_desktop_fixture(runtime, model.server_port, port)
                    guard = Path(temporary) / "guard"
                    audit = output / "logs/backend-network.log"
                    audit.parent.mkdir(parents=True, exist_ok=True)
                    write_guard(guard, audit)
                    env = desktop_environment(runtime, python, guard)
                    install_env = {key: value for key, value in env.items() if key != "PYTHONPATH"}
                    metadata = subprocess.check_output(
                        [str(python), "-I", "-c", "import sys,json; print(json.dumps({'prefix':sys.prefix}))"],
                        cwd=runtime, env=install_env, timeout=10, text=True)
                    expected_prefix = json.loads(metadata)["prefix"]
                    npm = shutil.which("npm.cmd" if sys.platform == "win32" else "npm", path=env.get("PATH", env.get("Path")))
                    if not npm:
                        raise RuntimeError("Node 22 and npm are required")
                    # Install only into this desktop copy AFTER backend acceptance.
                    run_bounded([npm, "ci", "--no-audit", "--no-fund"], output / "logs/npm-ci.log",
                                cwd=runtime, env=install_env, timeout=300)
                    result["sandbox"] = ("explicit-local-root-no-sandbox" if allow_no_sandbox else
                                         prepare_sandbox(runtime, output, env))
                    env.update(SHIKIGAMI_SMOKE_RUNTIME=str(runtime), SHIKIGAMI_SMOKE_OUTPUT=str(output),
                               SHIKIGAMI_SMOKE_PORT=str(port), SHIKIGAMI_SMOKE_PROFILE=PROFILE_ID,
                               SHIKIGAMI_SMOKE_REPLY=REPLY)
                    if port_open(port):
                        raise RuntimeError("Ephemeral backend port acquired an owner before Electron launch")
                    command = electron_command(runtime, Path(__file__).with_suffix(".cjs"),
                                               allow_no_sandbox=allow_no_sandbox)
                    try:
                        run_bounded(command, output / "logs/electron.log", cwd=runtime, env=env, timeout=110)
                        evidence = read_renderer_evidence(output, port)
                        loaded = json.loads((guard / "loaded.json").read_text(encoding="utf-8"))
                        write_json(output / "python.json", loaded)
                        validate_python_evidence(loaded, python, evidence["backend_pid"], expected_prefix)
                        if FixtureModel.calls != 1 or (audit.exists() and audit.stat().st_size):
                            raise RuntimeError("Unexpected model calls or forbidden network/process operation")
                        result.update(ok=True, checks=CHECKS, renderer=evidence, provider_calls=FixtureModel.calls)
                    finally:
                        cleanup_backend(output, port)
            finally:
                model.shutdown()
                thread.join(5)
    except Exception as error:
        result.update(ok=False, error=str(error))
    write_json(output / "acceptance.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "python", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--allow-no-sandbox", action="store_true",
                        help="explicit local Linux root testing only; never used by hosted CI")
    args = parser.parse_args(argv)
    result = run_smoke(args.source, args.python, args.output, allow_no_sandbox=args.allow_no_sandbox)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
