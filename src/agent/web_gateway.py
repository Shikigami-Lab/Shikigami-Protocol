"""Anonymous native web retrieval in a disposable, network-only container.

The caller owns run authorization. This bridge accepts only a URL, never forwards
host credentials, and returns the pinned SDK's WebFetchResult without log text.

DNS disclosure: only when ALL system answers are in 198.18.0.0/15, this bridge
automatically sends the destination hostname (not URL path/query) to Google's
https://dns.google/resolve JSON DNS endpoint, without credentials or cookies.
Public answers are pinned in this one helper's /etc/hosts. No OS DNS/proxy
settings change, and webpage HTTP remains entirely in the native provider.
"""
import asyncio
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid

import httpx

FETCH_TIMEOUT_SECONDS = 60
MAX_RESULT_BYTES = 2_000_000
MAX_REQUEST_BYTES = 8192
MAX_DIAGNOSTIC_BYTES = 16_384
DOCKER_TIMEOUT_SECONDS = 10
DNS_TIMEOUT_SECONDS = 5
MAX_DOH_BYTES = 65_536
DOH_ENDPOINT = 'https://dns.google/resolve'
FAKE_IP_NETWORK = ipaddress.ip_network('198.18.0.0/15')


def _public_addresses(values):
    if not values or len(values) > 128:
        raise ValueError('DNS returned an empty or excessive answer set')
    addresses = []
    for value in values:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            raise ValueError('DNS returned an invalid address') from None
        if not ip.is_global or ip.is_multicast or ip.is_unspecified or '%' in str(ip):
            raise ValueError('DNS returned a non-public destination')
        addresses.append(str(ip))
    return list(dict.fromkeys(addresses))


def _parse_doh_result(raw, hostname):
    if len(raw) > MAX_DOH_BYTES:
        raise ValueError('DNS response exceeded its size limit')
    try:
        result = json.loads(raw)
        questions = result.get('Question')
        answers = result.get('Answer')
        if (type(result.get('Status')) is not int or result['Status'] != 0
                or result.get('TC', False) is not False
                or not isinstance(questions, list) or len(questions) != 1
                or questions[0].get('type') != 1
                or questions[0].get('name', '').lower().rstrip('.') != hostname.rstrip('.')
                or not isinstance(answers, list) or len(answers) > 128):
            raise ValueError()
        values = []
        for answer in answers:
            if not isinstance(answer, dict) or type(answer.get('type')) is not int:
                raise ValueError()
            if answer['type'] == 1:
                value = answer.get('data')
                if not isinstance(value, str) or ipaddress.ip_address(value).version != 4:
                    raise ValueError()
                values.append(value)
        return _public_addresses(values)
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise ValueError('DNS response did not contain a valid public A answer set') from None


async def _doh_addresses(hostname):
    # Fresh, credential-free client: no environment proxy, netrc, cookie store,
    # redirect following or retry. This HTTP call is DNS only, never webpage IO.
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                timeout=DNS_TIMEOUT_SECONDS) as client:
        async with client.stream('GET', DOH_ENDPOINT, params={'name': hostname, 'type': 'A'},
                                 headers={'Accept': 'application/dns-json', 'Accept-Encoding': 'identity'}) as response:
            if response.status_code != 200:
                raise ValueError('DNS service did not return a successful response')
            raw = bytearray()
            async for chunk in response.aiter_raw():
                if len(raw) + len(chunk) > MAX_DOH_BYTES:
                    raise ValueError('DNS response exceeded its size limit')
                raw.extend(chunk)
            return _parse_doh_result(raw, hostname)


async def _bounded_dns(run, operation):
    pending = asyncio.create_task(operation)
    deadline = time.monotonic() + DNS_TIMEOUT_SECONDS
    try:
        while True:
            if run.stopped.is_set():
                raise asyncio.CancelledError()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Web DNS resolution timed out')
            done, _ = await asyncio.wait((pending,), timeout=min(.05, remaining))
            if done:
                return pending.result()
    finally:
        if not pending.done():
            pending.cancel()
        # Join async resolver/HTTP cleanup even on repeated parent cancellation.
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not pending.cancelled():
            pending.exception()


async def _resolve_destination(run, url):
    host = httpx.URL(url).raw_host.decode('ascii').lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return '', []  # A literal already passed host validation; native rechecks.
    try:
        records = await _bounded_dns(run, asyncio.get_running_loop().getaddrinfo(
            host.rstrip('.'), None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM))
        addresses = []
        for family, _, _, _, address in records:
            ip = ipaddress.ip_address(address[0])
            if family not in (socket.AF_INET, socket.AF_INET6) or ip.version != (4 if family == socket.AF_INET else 6):
                raise ValueError('DNS returned an invalid address family')
            addresses.append(str(ip))
        if addresses and all(ipaddress.ip_address(ip) in FAKE_IP_NETWORK for ip in addresses):
            addresses = await _bounded_dns(run, _doh_addresses(host.rstrip('.')))
        # Mixed Fake-IP/public/private answers are rejected, never filtered or
        # used to trigger DoH. The native provider still validates these pins.
        return host, _public_addresses(addresses)
    except TimeoutError:
        raise
    except (OSError, httpx.HTTPError):
        raise RuntimeError('Web DNS resolution failed') from None


def _request(body):
    if not isinstance(body, dict) or set(body) != {"url"}:
        raise ValueError("Web retrieval accepts only a url")
    url = body["url"]
    if (not isinstance(url, str) or not url or len(url) > 4096
            or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url):
        raise ValueError("Invalid webpage URL")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
        if (parsed.scheme not in ("http", "https") or not host or "@" in parsed.netloc
                or "%" in host or (port is not None and not 1 <= port <= 65535)):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Reject ambiguous IPv4 spellings (octal/hex/integer/short forms),
            # single-label and local names before starting Docker. DNS and
            # redirects are validated and pinned by the native HTTP provider.
            ascii_host = host.encode("idna").decode("ascii")
            if ("." not in ascii_host or ascii_host.endswith((".localhost", ".local", ".internal", ".localdomain", ".home", ".lan"))
                    or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", ascii_host)
                    or len(ascii_host) > 253
                    or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                           for label in ascii_host.split("."))):
                raise ValueError()
        else:
            if not address.is_global or address.is_multicast:
                raise ValueError()
    except (ValueError, UnicodeError):
        raise ValueError("Only public HTTP(S) URLs without credentials are allowed") from None
    payload = (json.dumps({"url": url}, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("Web retrieval request is too large")
    return payload


def _hidden():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _create_command(name, bundle_root, image, pinned_hosts=()):
    bundle = Path(bundle_root).resolve()
    # Docker --mount is comma-delimited; never reinterpret a path as options.
    if "," in str(bundle) or not bundle.is_dir():
        raise ValueError("Invalid native web integration bundle")
    return ["docker", "create", "--pull=never", "-i", "--name", name,
            "--network", "default", "--read-only", "--user", "1000:1000",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "128",
            "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=256m",
            "--tmpfs", "/home/tester:rw,nosuid,nodev,uid=1000,gid=1000,size=64m",
            "--workdir", "/tmp", "--env", "DSH_TELEMETRY_DISABLED=1",
            "--mount", f"type=bind,source={bundle},target=/opt/agent,readonly",
            *[arg for hostname, address in pinned_hosts for arg in ('--add-host', f'{hostname}:{address}')],
            image, "python", "/opt/agent/fetch.py"]


def _result(raw):
    try:
        result = json.loads(raw)
        body = result.get("body") if isinstance(result, dict) else None
        if (not isinstance(result, dict) or set(result) != {"url", "statusCode", "body", "truncated"}
                or not isinstance(result["url"], str)
                or type(result["statusCode"]) is not int or not 100 <= result["statusCode"] <= 599
                or type(result["truncated"]) is not bool
                or not isinstance(body, dict) or set(body) != {"kind", "content"}
                or body["kind"] not in ("text", "html") or not isinstance(body["content"], str)):
            raise ValueError()
        return result
    except (ValueError, UnicodeError, TypeError):
        raise RuntimeError("Invalid native webpage retrieval result") from None


def _fetch_blocking(run, payload, bundle_root, image, cancelled, pinned_hosts, deadline):
    # Only this worker owns the Docker processes. The async caller must join it
    # on cancellation; killing a docker CLI alone would leave its container.
    name = "shikigami-web-" + uuid.uuid4().hex
    command = _create_command(name, bundle_root, image, pinned_hosts)
    process = None
    readers = []
    output = bytearray()
    overflow = threading.Event()
    read_failed = threading.Event()

    def check():
        if cancelled.is_set() or run.stopped.is_set():
            raise asyncio.CancelledError()
        if time.monotonic() >= deadline:
            raise TimeoutError("Native webpage retrieval timed out")
        if overflow.is_set() or read_failed.is_set():
            raise RuntimeError("Native webpage retrieval exceeded output limits or failed")

    def read(stream, limit, retain):
        count = 0
        try:
            while True:
                part = stream.read(8192)
                if not part:
                    return
                count += len(part)
                if count > limit:
                    overflow.set()
                    return
                if retain:
                    output.extend(part)
        except (OSError, ValueError):
            read_failed.set()

    def send():
        try:
            # The request can exceed an OS pipe buffer. Never block the owner
            # thread here, or cancellation could not remove a stuck helper.
            remaining = memoryview(payload)
            while remaining:
                written = process.stdin.write(remaining)
                if not written:
                    raise OSError("Native web input closed")
                remaining = remaining[written:]
            process.stdin.close()
        except (OSError, ValueError):
            read_failed.set()

    check()
    try:
        creation = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=DOCKER_TIMEOUT_SECONDS, **_hidden())
        if creation.returncode:
            raise RuntimeError("Native web helper could not be created")
        check()
        process = subprocess.Popen(["docker", "start", "--attach", "--interactive", name],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   bufsize=0, **_hidden())
        for stream, limit, retain in ((process.stdout, MAX_RESULT_BYTES, True),
                                      (process.stderr, MAX_DIAGNOSTIC_BYTES, False)):
            reader = threading.Thread(target=read, args=(stream, limit, retain), daemon=True)
            reader.start()
            readers.append(reader)
        writer = threading.Thread(target=send, daemon=True)
        writer.start()
        readers.append(writer)
        while process.poll() is None:
            check()
            cancelled.wait(.05)
        for reader in readers:
            reader.join(timeout=max(0, deadline - time.monotonic()))
        check()
        if process.returncode:
            raise RuntimeError("Native webpage retrieval failed")
        return _result(output)
    except subprocess.TimeoutExpired:
        raise TimeoutError("Native web helper startup timed out") from None
    except TimeoutError:
        raise
    except OSError:
        raise RuntimeError("Native web helper could not start") from None
    finally:
        # Even a failed/timed-out create may have reached the daemon. Remove
        # only our unguessable name; never enumerate or stop other containers.
        cleanup_error = None
        try:
            removal = subprocess.run(["docker", "rm", "--force", name], capture_output=True,
                                     timeout=DOCKER_TIMEOUT_SECONDS, **_hidden())
            if removal.returncode and b"no such container" not in removal.stderr.lower():
                cleanup_error = RuntimeError("Native web helper cleanup failed")
        except (OSError, subprocess.TimeoutExpired):
            cleanup_error = RuntimeError("Native web helper cleanup failed")
        finally:
            if process is not None:
                try:
                    process.wait(timeout=DOCKER_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                for reader in readers:
                    reader.join(timeout=1)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
        if cleanup_error:
            raise cleanup_error


async def native_fetch(run, body, bundle_root, image):
    """Return native WebFetchResult; raise ValueError/RuntimeError/TimeoutError.

    Cancellation (task.cancel or run.stopped) raises CancelledError only after
    removing the dedicated helper and waiting for its attached Docker process.
    No model request, fetch retry, host retrieval fallback or workspace mount.
    """
    payload = _request(body)
    if run.stopped.is_set():
        raise asyncio.CancelledError()
    deadline = time.monotonic() + FETCH_TIMEOUT_SECONDS
    host, addresses = await _resolve_destination(run, body['url'])
    if run.stopped.is_set():
        raise asyncio.CancelledError()
    aliases = dict.fromkeys((host, host.rstrip('.'))) if host else ()
    pinned_hosts = [(alias, address) for alias in aliases for address in addresses]
    cancelled = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(_fetch_blocking, run, payload, bundle_root, image,
                                                 cancelled, pinned_hosts, deadline))
    try:
        result = await asyncio.shield(worker)
        if run.stopped.is_set():
            raise asyncio.CancelledError()
        return result
    except asyncio.CancelledError:
        cancelled.set()
        # Repeated cancellation must not release the caller while cleanup is
        # running in the worker. Docker cleanup errors remain explicit failures.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        if not worker.cancelled():
            worker.result()
        raise
