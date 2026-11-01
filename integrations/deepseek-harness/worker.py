"""Container-only SDK launcher and byte transport; no execution loop or secrets."""
import base64
import importlib.metadata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from deepseek_harness import DeepSeekHarness

_output_lock = threading.Lock()
_pending = {}
_controls = queue.Queue()


def emit(item):
    with _output_lock:
        print(json.dumps(item, ensure_ascii=False), flush=True)


def _receive():
    for line in sys.stdin:
        item = json.loads(line)
        if item.get("kind") == "control":
            _controls.put(item)
        elif item.get("id") in _pending:
            _pending[item["id"]].put(item)


class _Proxy(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _forward(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 8_000_000:
            self.send_error(413)
            return
        rid = str(uuid.uuid4())
        replies = queue.Queue()
        _pending[rid] = replies
        raw = self.rfile.read(length)
        emit({"kind": self.server.channel, "id": rid, "method": self.command,
              "path": self.path, "body": json.loads(raw) if raw else None,
              "headers": {k: v for k, v in self.headers.items() if k.lower() in (
                  "accept", "content-type", "mcp-protocol-version", "mcp-session-id")}})
        try:
            start = replies.get(timeout=120)
            self.send_response(start["status"])
            self.send_header("Content-Type", start.get("content_type", "application/json"))
            for key, value in start.get("headers", {}).items():
                if key.lower() in ("mcp-session-id", "mcp-protocol-version"):
                    self.send_header(key, value)
            self.end_headers()
            while True:
                item = replies.get(timeout=120)
                if item["kind"] == "response_end":
                    break
                self.wfile.write(base64.b64decode(item["data"]))
                self.wfile.flush()
        except (queue.Empty, BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            _pending.pop(rid, None)

    do_POST = _forward
    do_GET = _forward
    do_DELETE = _forward


def _local(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:8091" + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read())


def main():
    for package in ('deepseek-harness-sdk', 'deepseek-harness-runtime-bin'):
        if importlib.metadata.version(package) != '0.2.1rc2':
            raise RuntimeError('Unsupported Harness runtime version; rebuild the pinned image')
    config = json.loads(sys.stdin.readline())
    threading.Thread(target=_receive, daemon=True).start()
    servers = []
    for port, channel in ((8090, "model_request"), (8092, "mcp_request"), (8093, "web_request")):
        proxy = ThreadingHTTPServer(("127.0.0.1", port), _Proxy)
        proxy.channel = channel
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        servers.append(proxy)
    route = config["route"]
    provider = {"apiKeyEnv": "SHIKI_PROXY_KEY", "baseURL": "http://127.0.0.1:8090/" + (
                    "v1beta" if route["native_google"] else "v1"),
                "retryPolicy": {"mode": "normal", "maxRetries": 0},
                "models": [{"id": route["model"], "contextWindow": 128000, "maxTokens": 4096}]}
    if not route["native_google"]:
        provider["api"] = "openai-completions"
    insert = [{"id": "shikigami-agent", "name": "/opt/agent/agent.mjs"}]
    if config["timer_enabled"] or config.get('business_tools'):
        insert.insert(0, {"id": "mcp-shikigami", "name": "@deepseek-ai/dsh-mcp-client", "config": {
            "serverName": "shikigami", "transport": "streamable-http", "url": "http://127.0.0.1:8092/mcp",
            "failOnStartupError": True, "reconnect": {"enabled": False}, "toolCallTimeoutMs": 30000}})
    patch = [{"id": "system-prompt", "config": {"includeHarnessIdentity": False, "includeRuntimeContext": False}},
             {"id": "llm-pi-ai", "config": {"providers": {route["provider"]: provider}}}, {"insert": insert}]
    if 'web_fetch' in config.get('native_tools', []):
        patch.append({'id': 'web', 'config': {'searchProvider': 'deepseek-official', 'fetchProvider': 'shikigami-bridge'}})
    Path("/tmp/shikigami.patch.json").write_text(json.dumps(patch), encoding="utf-8")
    try:
        with DeepSeekHarness(dsh_home="/harness", cwd="/workspace", profile="sdk", provider=route["provider"],
                model=route["model"], patches=("/tmp/shikigami.patch.json",), max_tokens=4096,
                env={"SHIKI_PROXY_KEY": "host-credentials-not-shared", "SHIKI_MODEL": route["model"], "SHIKI_PROVIDER": route["provider"],
                     'DSH_PERMISSION_MODE': 'danger-full-access'},
                initialize_timeout_seconds=60, request_timeout_seconds=120):
            _local("/start", {"envelope": config["envelope"], "timer_enabled": config["timer_enabled"],
                'native_tools': config.get('native_tools', []), 'business_tools': config.get('business_tools', [])})
            done = False
            while not done:
                while not _controls.empty():
                    command = _controls.get_nowait()
                    try:
                        result = _local("/" + command["action"], command.get("body") or {})
                        emit({"kind": "control_result", "id": command["id"], **result})
                    except Exception:
                        emit({"kind": "control_result", "id": command["id"], "accepted": False})
                for event in _local("/events"):
                    emit({"kind": "event", "event": event})
                    if event["type"] in ("completed", "cancelled", "failed"):
                        done = True
                time.sleep(.03)
    finally:
        for proxy in servers:
            proxy.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # The worker never receives real credentials. Bound diagnostics for the host.
        emit({"kind": "fatal", "message": str(error)[:2000]})
        raise
