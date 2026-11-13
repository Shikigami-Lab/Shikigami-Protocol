"""Run real source-package startup, local-model chat and synthetic legacy upgrade."""
import argparse
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

PROFILE_ID = "package-acceptance"
LEGACY_MESSAGES = [(41, "user", "Synthetic original user message"),
                   (42, "assistant", "Synthetic original character reply")]
REPLY = "Synthetic package acceptance reply."
STARTUP_TIMEOUT = 45
STOP_TIMEOUT = 5

AUDIT_GUARD = """
def guard(event, args):
    blocked = event == 'subprocess.Popen'
    # Python's platform.platform(), used by SDK request headers, probes uname on Linux.
    if event == 'subprocess.Popen' and args[0] == 'uname' and args[1] == ['uname', '-p']:
        blocked = False
    if event in ('socket.connect', 'socket.bind'):
        address = args[1]
        blocked = not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1')
    if event == 'socket.getaddrinfo':
        blocked = args[0] not in ('localhost', '127.0.0.1', '::1')
    if blocked:
        with audit_path.open('a', encoding='utf-8') as file:
            detail = {'event': event}
            if event == 'subprocess.Popen':
                detail['command'] = str(args[1])[:300]
            file.write(json.dumps(detail) + '\\n')
        raise PermissionError('Acceptance permits loopback fixture traffic only')
sys.addaudithook(guard)
"""

BOOTSTRAP = """
import sys, socket, json
from pathlib import Path
source, audit_path = Path(sys.argv[1]), Path(sys.argv[2])
app_port = int(sys.argv[3])
""" + AUDIT_GUARD + """
sys.path.insert(0, str(source))
from src.config.app_config import AppConfig
original_load = AppConfig.load
def isolated_config(cls, *args, **kwargs):
    config = original_load(*args, **kwargs)
    config.reflection = {'enabled': False}
    config.ase = {'enabled': False}
    config.agent = {'enabled': False}
    config.memory = {'enabled': False, 'day_summary_enabled': False, 'embedding': {'enabled': False}}
    config.engines = {name: {'enabled': False} for name in ('emotion', 'affinity', 'energy')}
    config.topic_discovery = {'enabled': False}
    config.stt = {'enabled': False}
    return config
AppConfig.load = classmethod(isolated_config)
import uvicorn
from server import app
uvicorn.run(app, host='127.0.0.1', port=app_port, log_level='warning')
"""


def validate_paths(source, python, output):
    # POSIX venv interpreters are symlinks; resolving them loses the venv context.
    source, python, output = Path(source).resolve(), Path(python).absolute(), Path(output).resolve()
    if not source.is_dir() or not python.is_file():
        raise ValueError("Source folder and installed Python interpreter are required")
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Acceptance output must be separate from source")
    if (output / "acceptance.json").exists():
        raise ValueError("Acceptance output already contains evidence")
    return source, python, output


def child_environment(source):
    denied = {"PYTHONPATH", "PYTHONHOME", "SHIKIGAMI_APP_ROOT", "PIP_INDEX_URL", "UV_INDEX_URL"}
    env = {key: value for key, value in os.environ.items()
           if key not in denied and "PROXY" not in key.upper()
           and not key.upper().endswith(("_API_KEY", "_TOKEN"))}
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    return env


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class LoopbackClient:
    def __init__(self, port):
        self.base = "http://127.0.0.1:" + str(port)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, payload=None):
        if (not path.startswith("/") or path.startswith("//") or "\\" in path
                or any(ord(char) < 32 for char in path)):
            raise ValueError("Expected a local application path")
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError("Application returned HTTP " + str(response.status))
                body = response.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024:
                    raise RuntimeError("Acceptance response exceeded its limit")
                return body.decode("utf-8")
        except urllib.error.HTTPError as error:
            raise RuntimeError("Application returned HTTP " + str(error.code)) from None


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class AppServer:
    def __init__(self, source, python, logs, name):
        self.source, self.python = Path(source), Path(python)
        self.logs, self.name = Path(logs), name
        self.port = free_port()
        self.process, self.log = None, None
        self.client = LoopbackClient(self.port)

    def __enter__(self):
        self.logs.mkdir(parents=True, exist_ok=True)
        self.log = (self.logs / (self.name + ".log")).open("wb")
        audit = self.logs / (self.name + "-network.log")
        try:
            self.process = subprocess.Popen(
                [str(self.python), "-I", "-c", BOOTSTRAP, str(self.source), str(audit),
                 str(self.port)],
                cwd=self.source, env=child_environment(self.source),
                stdout=self.log, stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + STARTUP_TIMEOUT
            while time.monotonic() < deadline:
                code = self.process.poll()
                if code is not None:
                    raise RuntimeError("Application startup exited with " + str(code))
                try:
                    version = json.loads(self.client.request("/version"))
                    expected = json.loads((self.source / "package.json").read_text(encoding="utf-8"))["version"]
                    if version.get("version") != expected:
                        raise RuntimeError("Application version differs from source")
                    self.client.request("/")
                    return self
                except (OSError, ValueError, urllib.error.URLError):
                    time.sleep(0.1)
            raise RuntimeError("Application startup timed out")
        except BaseException:
            self.stop()
            raise

    def stop(self):
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(STOP_TIMEOUT)
        finally:
            if self.log:
                self.log.close()

    def __exit__(self, *args):
        self.stop()


def read_database(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        rows = db.execute("SELECT id,role,content FROM messages ORDER BY id").fetchall()
        return rows, columns


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def write_fixture(source, model_port, legacy=False):
    source = Path(source)
    config = {
        "host": "127.0.0.1", "log_level": "warning", "default_llm": "acceptance_local",
        "llm_presets": {"acceptance_local": {
            "type": "openai_compat", "api_key": "synthetic-package-key",
            "base_url": "http://127.0.0.1:" + str(model_port) + "/v1", "model": "synthetic-model",
            "unsupported_params": ["temperature", "top_p", "presence_penalty", "frequency_penalty"],
        }},
        "memory": {"enabled": False}, "reflection": {"enabled": False}, "ase": {"enabled": False},
        "agent": {"enabled": False}, "stt": {"enabled": False}, "custom_fixture_setting": "preserved",
    }
    write_json(source / "config/app.yaml", config)
    write_json(source / "config/sessions_meta.json", {"current_id": PROFILE_ID, "profile_order": [PROFILE_ID]})
    write_json(source / "profiles" / (PROFILE_ID + ".json"), {
        "profile_id": PROFILE_ID, "display_name": "Synthetic character", "base_prompt": "Synthetic authored persona",
        "load_into_chat": True, "user_portrait": {"content": "Synthetic authored portrait", "updated_at": 1},
        "user_portrait_config": {"enabled": False}, "persona_evolution_config": {"enabled": False},
        "memory_config": {"enabled": False}, "custom_fixture_field": "preserved",
    })
    if legacy:
        storage = source / "profiles" / PROFILE_ID
        storage.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(storage / "chat_records.db")) as db:
            db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL, timestamp REAL NOT NULL)")
            db.executemany("INSERT INTO messages VALUES(?,?,?,1)", LEGACY_MESSAGES)
            db.commit()
        write_json(storage / "long_term_facts.json", {"facts": [
            {"id": "f_fixture", "content": "Synthetic preserved preference", "source": "manual",
             "is_manual": True, "category": "preference", "weight": 1.0, "updated_at": 1},
        ]})
        note = storage / "workspace/fixture.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("Synthetic authored workspace note.\n", encoding="utf-8")


class FixtureModel(BaseHTTPRequestHandler):
    calls = 0
    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if self.path != "/v1/chat/completions" or not 0 < size <= 2 * 1024 * 1024:
            self.send_error(400)
            return
        payload = json.loads(self.rfile.read(size))
        if payload.get("model") != "synthetic-model" or not payload.get("stream"):
            self.send_error(400)
            return
        type(self).calls += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1,
                 "model": "synthetic-model",
                 "choices": [{"index": 0, "delta": {"content": REPLY}, "finish_reason": None}]}
        self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
        self.wfile.flush()

    def log_message(self, *args):
        pass


def check_chat(client):
    body = client.request("/chat", {"session_id": PROFILE_ID, "message": "Synthetic test request", "conversation_only": True})
    frames = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    if "".join(frame.get("token", "") for frame in frames) != REPLY or not any(frame.get("done") for frame in frames):
        raise RuntimeError("Real chat endpoint did not deliver the fixture response")
    history = json.loads(client.request("/sessions/" + PROFILE_ID + "/history"))["messages"]
    if not any(row["role"] == "assistant" and row["content"] == REPLY for row in history):
        raise RuntimeError("The chat reply was not persisted")


def snapshot_authored(source):
    names = ["config/app.yaml", "profiles/" + PROFILE_ID + ".json"]
    note = "profiles/" + PROFILE_ID + "/workspace/fixture.md"
    if (source / note).exists():
        names.append(note)
    return {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names}


def run_acceptance(source, python, output):
    source, python, output = validate_paths(source, python, output)
    output.mkdir(parents=True, exist_ok=True)
    logs = output / "logs"
    FixtureModel.calls = 0
    with ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel) as model:
        thread = threading.Thread(target=model.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="package-runtime-", dir=output) as temporary:
                runtimes = Path(temporary)
                for scenario in ("fresh", "upgrade"):
                    runtime = runtimes / scenario
                    shutil.copytree(source, runtime)
                    if scenario == "fresh":
                        with AppServer(runtime, python, logs, "fresh-defaults"):
                            pass
                    write_fixture(runtime, model.server_port, legacy=scenario == "upgrade")
                    before = snapshot_authored(runtime)
                    with AppServer(runtime, python, logs, scenario) as app:
                        check_chat(app.client)
                    with AppServer(runtime, python, logs, scenario + "-restart") as app:
                        history = json.loads(app.client.request("/sessions/" + PROFILE_ID + "/history"))["messages"]
                        if not any(row["content"] == REPLY for row in history):
                            raise RuntimeError("Restart lost the saved chat reply")
                    if snapshot_authored(runtime) != before:
                        raise RuntimeError("Authored configuration, persona, portrait or workspace was changed")
                    if scenario == "upgrade":
                        storage = runtime / "profiles" / PROFILE_ID
                        rows, columns = read_database(storage / "chat_records.db")
                        if rows[:2] != LEGACY_MESSAGES or not {"agent_run_id", "generation_status"} <= columns:
                            raise RuntimeError("Legacy message migration lost original data")
                        facts = json.loads((storage / "long_term_facts.json").read_text(encoding="utf-8"))["facts"]
                        if not any(row.get("id") == "f_fixture" and row.get("content") == "Synthetic preserved preference" for row in facts):
                            raise RuntimeError("Legacy fact was not preserved")
        finally:
            model.shutdown()
            thread.join(5)
    if FixtureModel.calls != 2:
        raise RuntimeError("Unexpected model requests outside the two fixture chats")
    blocked = [path for path in logs.glob("*-network.log") if path.stat().st_size]
    if blocked:
        raise RuntimeError("The package attempted a forbidden network or process operation")
    return {"ok": True, "synthetic_only": True, "version": json.loads((source / "package.json").read_text(encoding="utf-8"))["version"],
            "checks": ["fresh-default-startup", "fresh-chat-and-restart", "legacy-migration-chat-and-restart",
                       "authored-data-preserved", "loopback-model-only"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        source, python, output = validate_paths(args.source, args.python, args.output)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    output.mkdir(parents=True, exist_ok=True)
    version = json.loads((source / "package.json").read_text(encoding="utf-8"))["version"]
    try:
        result = run_acceptance(source, python, output)
    except Exception as error:
        result = {"ok": False, "synthetic_only": True, "version": version, "error": str(error)}
    write_json(output / "acceptance.json", result)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
