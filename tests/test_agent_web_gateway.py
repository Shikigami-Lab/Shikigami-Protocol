"""Native web bridge guards and lifecycle; no Docker or network in unit tests."""
import asyncio
import io
import json
import os
import runpy
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx


RESULT = {"url": "https://example.com/", "statusCode": 200,
          "body": {"kind": "html", "content": "<h1>Example Domain</h1>"}, "truncated": False}


class FakeProcess:
    def __init__(self, payload=None, running=False):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(payload if payload is not None else json.dumps(RESULT).encode())
        self.stderr = io.BytesIO()
        self.returncode = None if running else 0
        self.waited = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waited = True
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def kill(self):
        self.returncode = -9


class WebGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.agent import web_gateway
        self.gateway = web_gateway
        self.run = SimpleNamespace(id="test-run", stopped=threading.Event())
        self.bundle = Path(__file__).resolve().parents[1] / "integrations" / "deepseek-harness"
        self.image = "shikigami-harness:workspace-0.2.1rc2"
        self.proc = FakeProcess()
        self.commands = []
        self.started = threading.Event()
        self.removed = threading.Event()
        self.allow_cleanup = threading.Event()
        self.allow_cleanup.set()
        dns = patch.object(asyncio.get_running_loop(), 'getaddrinfo',
                           new=AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 0))]))
        dns.start()
        self.addCleanup(dns.stop)

        def command(args, **kwargs):
            self.commands.append((args, kwargs))
            if args[1] == "rm":
                self.allow_cleanup.wait(3)
                self.proc.returncode = 137 if self.proc.returncode is None else self.proc.returncode
                self.removed.set()
            return subprocess.CompletedProcess(args, 0, b"container-id\n", b"")

        def spawn(args, **kwargs):
            self.commands.append((args, kwargs))
            self.started.set()
            return self.proc

        for target, replacement in (("subprocess.run", command), ("subprocess.Popen", spawn)):
            guard = patch.object(web_gateway.subprocess, target.split(".")[1], side_effect=replacement)
            guard.start()
            self.addCleanup(guard.stop)

    async def fetch(self, body=None):
        return await self.gateway.native_fetch(self.run, {"url": "https://example.com/"} if body is None else body,
                                               self.bundle, self.image)

    async def wait_started(self):
        self.assertTrue(await asyncio.to_thread(self.started.wait, 2))

    async def test_native_result_and_container_isolation(self):
        self.assertEqual(await self.fetch(), RESULT)
        create = self.commands[0][0]
        self.assertEqual(create[:2], ["docker", "create"])
        from src.agent.container_identity import container_identity
        uid, gid = container_identity()
        for flag, value in (("--network", "default"), ("--user", f"{uid}:{gid}"),
                            ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges"),
                            ("--memory", "512m"), ("--cpus", "1"), ("--pids-limit", "128")):
            self.assertEqual(create[create.index(flag) + 1], value)
        self.assertIn("--read-only", create)
        self.assertIn("--pull=never", create)
        self.assertEqual(create.count("--mount"), 1)
        self.assertIn("readonly", create[create.index("--mount") + 1])
        self.assertEqual(create[-3:], [self.image, "python", "/opt/agent/fetch.py"])
        self.assertNotIn("/workspace", " ".join(create))
        self.assertNotIn("--env-file", create)
        self.assertNotIn("--publish", create)
        self.assertIn('example.com:93.184.215.14', create)
        name = create[create.index("--name") + 1]
        self.assertEqual(self.commands[-1][0], ["docker", "rm", "--force", name])
        self.assertTrue(self.proc.waited)

    async def test_invalid_requests_do_not_spawn(self):
        invalid = [[], "https://example.com", {}, {"url": 42}, {"url": "https://example.com", "headers": {}},
                   {"url": "https://example.com/" + "a" * 4096}]
        urls = ["file:///etc/passwd", "ftp://example.com", "https://u:p@example.com", "https://@example.com",
                "http://localhost", "http://sub.localhost.", "http://host.docker.internal", "http://printer.local",
                "http://127.0.0.1", "http://127.1", "http://2130706433", "http://0x7f000001",
                "http://0177.0.0.1", "http://10.0.0.1", "http://169.254.169.254", "http://100.64.0.1",
                "http://[::1]", "http://[::ffff:127.0.0.1]", "http://[fe80::1%25eth0]",
                "http://example.com:99999", "http://example.com\\@127.0.0.1", "https://example.com\n",
                "http://%31%32%37.0.0.1", "https:///example.com", "http://intranet", "http://224.0.0.1"]
        for body in invalid + [{"url": url} for url in urls]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                await self.fetch(body)
        self.assertFalse(self.commands)

    async def test_stopped_run_never_spawns(self):
        self.run.stopped.set()
        with self.assertRaises(asyncio.CancelledError):
            await self.fetch()
        self.assertFalse(self.commands)

    async def test_coroutine_cancellation_waits_for_container_removal_and_process(self):
        self.proc = FakeProcess(running=True)
        self.allow_cleanup.clear()
        task = asyncio.create_task(self.fetch())
        await self.wait_started()
        task.cancel()
        await asyncio.sleep(.1)
        self.assertFalse(task.done())
        task.cancel()  # A second parent cancellation must not detach cleanup.
        self.allow_cleanup.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        self.assertTrue(self.removed.is_set())
        self.assertTrue(self.proc.waited)
        self.assertEqual(sum(cmd[0][1] == "start" for cmd in self.commands), 1)

    async def test_run_stop_event_cleans_up(self):
        self.proc = FakeProcess(running=True)
        task = asyncio.create_task(self.fetch())
        await self.wait_started()
        self.run.stopped.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        self.assertTrue(self.removed.is_set())
        self.assertTrue(self.proc.waited)

    async def test_timeout_cleans_up_without_retry(self):
        self.proc = FakeProcess(running=True)
        with patch.object(self.gateway, "FETCH_TIMEOUT_SECONDS", .05):
            with self.assertRaises(TimeoutError):
                await self.fetch()
        self.assertTrue(self.removed.is_set())
        self.assertTrue(self.proc.waited)
        self.assertEqual(sum(cmd[0][1] == "create" for cmd in self.commands), 1)

    async def test_output_cap_and_malformed_or_non_native_result_fail_closed(self):
        for payload in [b"x" * 2_000_001, b"not-json", b'{"error":"private execution log"}', b'[]',
                        json.dumps({**RESULT, "statusCode": True}).encode(),
                        json.dumps({**RESULT, "body": {"kind": "binary", "content": "x"}}).encode()]:
            with self.subTest(size=len(payload)):
                self.proc = FakeProcess(payload=payload)
                with self.assertRaises(RuntimeError) as caught:
                    await self.fetch()
                self.assertNotIn("private execution log", str(caught.exception))
                self.assertTrue(self.removed.is_set())

    async def test_http_failure_status_is_still_a_native_result(self):
        result = {**RESULT, "statusCode": 404}
        self.proc = FakeProcess(json.dumps(result).encode())
        self.assertEqual(await self.fetch(), result)

    async def test_helper_nonzero_exit_is_not_retried(self):
        self.proc.returncode = 1
        with self.assertRaises(RuntimeError):
            await self.fetch()
        self.assertEqual(sum(cmd[0][1] == "start" for cmd in self.commands), 1)

    async def test_cancellation_during_blocked_stdin_is_bounded(self):
        stopped = threading.Event()

        class BlockedInput(io.BytesIO):
            def write(self, value):
                stopped.wait(2)
                return len(value)

        self.proc = FakeProcess(running=True)
        self.proc.stdin = BlockedInput()
        task = asyncio.create_task(self.fetch())
        await self.wait_started()
        task.cancel()
        await asyncio.sleep(.2)
        # Removal must happen even if the Docker client hasn't consumed stdin.
        removed_while_blocked = self.removed.is_set()
        stopped.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        self.assertTrue(removed_while_blocked)

    async def test_cancellation_during_create_waits_then_removes_without_start(self):
        entered, release = threading.Event(), threading.Event()

        def create_or_remove(args, **kwargs):
            self.commands.append((args, kwargs))
            if args[1] == "create":
                entered.set()
                release.wait(3)
            elif args[1] == "rm":
                self.removed.set()
            return subprocess.CompletedProcess(args, 0, b"id", b"")

        with patch.object(self.gateway.subprocess, "run", side_effect=create_or_remove):
            task = asyncio.create_task(self.fetch())
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            await asyncio.sleep(.05)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        self.assertTrue(self.removed.is_set())
        self.assertFalse(self.started.is_set())

    async def test_unicode_request_wire_size_is_bounded(self):
        with self.assertRaises(ValueError):
            await self.fetch({"url": "https://example.com/" + "界" * 3000})
        self.assertFalse(self.commands)

    async def test_stderr_output_is_bounded_and_never_returned(self):
        self.proc.stderr = io.BytesIO(b"private log " * 1600)
        with self.assertRaises(RuntimeError) as caught:
            await self.fetch()
        self.assertNotIn("private log", str(caught.exception))
        self.assertTrue(self.proc.waited)

    async def test_unique_helpers_for_same_run(self):
        await self.fetch()
        self.proc = FakeProcess()
        await self.fetch()
        commands = [cmd for cmd, _ in self.commands if cmd[1] == "create"]
        self.assertNotEqual(commands[0][commands[0].index("--name") + 1],
                            commands[1][commands[1].index("--name") + 1])

    async def test_cleanup_failure_is_not_reported_as_success(self):
        def command(args, **kwargs):
            if args[1] == "rm":
                return subprocess.CompletedProcess(args, 1, b"", b"daemon unavailable")
            return subprocess.CompletedProcess(args, 0, b"id", b"")

        with patch.object(self.gateway.subprocess, "run", side_effect=command):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                await self.fetch()
        self.assertTrue(self.proc.waited)

    async def test_stop_during_cleanup_does_not_return_stale_success(self):
        def command(args, **kwargs):
            if args[1] == "rm":
                self.run.stopped.set()
            return subprocess.CompletedProcess(args, 0, b"id", b"")

        with patch.object(self.gateway.subprocess, "run", side_effect=command):
            with self.assertRaises(asyncio.CancelledError):
                await self.fetch()

    async def test_create_timeout_is_sanitized_and_attempts_exact_name_cleanup(self):
        def command(args, **kwargs):
            self.commands.append((args, kwargs))
            if args[1] == 'create':
                raise subprocess.TimeoutExpired('private CLI diagnostic', 10)
            return subprocess.CompletedProcess(args, 0, b'id', b'')

        with patch.object(self.gateway.subprocess, 'run', side_effect=command):
            with self.assertRaises(TimeoutError) as caught:
                await self.fetch()
        self.assertNotIn('private CLI diagnostic', str(caught.exception))
        self.assertEqual([args[1] for args, _ in self.commands], ['create', 'rm'])
        self.assertFalse(self.started.is_set())

    async def test_cleanup_failure_after_uncertain_create_is_explicit(self):
        def command(args, **kwargs):
            if args[1] == 'create':
                raise subprocess.TimeoutExpired('create', 10)
            return subprocess.CompletedProcess(args, 1, b'', b'daemon unreachable')

        with patch.object(self.gateway.subprocess, 'run', side_effect=command):
            with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                await self.fetch()


class DnsResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.agent import web_gateway
        self.gateway = web_gateway
        self.run = SimpleNamespace(id='dns-test', stopped=threading.Event())
        self.loop = asyncio.get_running_loop()

    def answers(self, addresses):
        return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 (ip, 0, 0, 0) if ':' in ip else (ip, 0)) for ip in addresses]

    async def test_public_system_addresses_pin_without_doh(self):
        with patch.object(self.loop, 'getaddrinfo', new=AsyncMock(return_value=self.answers(['93.184.215.14']))), \
                patch.object(self.gateway, '_doh_addresses', new=AsyncMock()) as doh:
            host, addresses = await self.gateway._resolve_destination(self.run, 'https://example.com/')
        self.assertEqual((host, addresses), ('example.com', ['93.184.215.14']))
        doh.assert_not_awaited()

    async def test_fake_only_answers_use_one_doh_query(self):
        with patch.object(self.loop, 'getaddrinfo', new=AsyncMock(return_value=self.answers(['198.18.0.1', '198.19.255.254']))), \
                patch.object(self.gateway, '_doh_addresses', new=AsyncMock(return_value=['104.20.23.154', '172.66.147.243'])) as doh:
            host, addresses = await self.gateway._resolve_destination(self.run, 'https://example.com/')
        self.assertEqual(host, 'example.com')
        self.assertEqual(addresses, ['104.20.23.154', '172.66.147.243'])
        doh.assert_awaited_once_with('example.com')

    async def test_private_empty_or_mixed_answers_never_use_doh(self):
        for addresses in [[], ['127.0.0.1'], ['10.0.0.1'], ['93.184.215.14', '127.0.0.1'],
                          ['198.18.1.1', '93.184.215.14'], ['198.18.1.1', '10.0.0.1'], ['198.20.0.1', '198.18.1.1']]:
            with self.subTest(addresses=addresses), \
                    patch.object(self.loop, 'getaddrinfo', new=AsyncMock(return_value=self.answers(addresses))), \
                    patch.object(self.gateway, '_doh_addresses', new=AsyncMock()) as doh:
                with self.assertRaises(ValueError):
                    await self.gateway._resolve_destination(self.run, 'https://example.com/')
                doh.assert_not_awaited()

    async def test_doh_nonpublic_or_mixed_addresses_are_rejected(self):
        for addresses in [['10.0.0.1'], ['104.20.23.154', '127.0.0.1'], ['198.18.1.1'], []]:
            with self.subTest(addresses=addresses), \
                    patch.object(self.loop, 'getaddrinfo', new=AsyncMock(return_value=self.answers(['198.18.1.1']))), \
                    patch.object(self.gateway, '_doh_addresses', new=AsyncMock(return_value=addresses)):
                with self.assertRaises(ValueError):
                    await self.gateway._resolve_destination(self.run, 'https://example.com/')

    async def test_stop_during_resolution_is_bounded_and_never_spawns(self):
        entered = asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await asyncio.sleep(30)
        bundle = Path(__file__).resolve().parents[1] / 'integrations/deepseek-harness'
        with patch.object(self.loop, 'getaddrinfo', side_effect=slow), \
                patch.object(self.gateway.subprocess, 'run') as docker:
            task = asyncio.create_task(self.gateway.native_fetch(self.run, {'url':'https://example.com/'}, bundle, 'image'))
            await asyncio.wait_for(entered.wait(), 1)
            self.run.stopped.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, .5)
            docker.assert_not_called()

    async def test_cancellation_during_doh_closes_pending_operation(self):
        entered, finished = asyncio.Event(), asyncio.Event()
        async def slow(host):
            entered.set()
            try:
                await asyncio.sleep(30)
            finally:
                finished.set()
        with patch.object(self.loop, 'getaddrinfo', new=AsyncMock(return_value=self.answers(['198.18.1.1']))), \
                patch.object(self.gateway, '_doh_addresses', side_effect=slow):
            task = asyncio.create_task(self.gateway._resolve_destination(self.run, 'https://example.com/'))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, .5)
            self.assertTrue(finished.is_set())

    def doh_body(self, addresses=('104.20.23.154',), **fields):
        return json.dumps({'Status': 0, 'TC': False,
            'Question': [{'name': 'example.com.', 'type': 1}],
            'Answer': [{'name': 'example.com.', 'type': 1, 'data': ip} for ip in addresses],
            **fields}).encode()

    async def test_doh_request_fixed_endpoint_no_credentials_no_proxy_no_redirects(self):
        seen = []
        async def respond(request):
            seen.append(request)
            return httpx.Response(200, stream=httpx.ByteStream(self.doh_body()))
        original_client = httpx.AsyncClient
        def client(**kwargs):
            self.assertFalse(kwargs['trust_env'])
            self.assertFalse(kwargs['follow_redirects'])
            self.assertEqual(kwargs['timeout'], 5)
            return original_client(transport=httpx.MockTransport(respond), **kwargs)
        with patch.object(self.gateway.httpx, 'AsyncClient', side_effect=client):
            self.assertEqual(await self.gateway._doh_addresses('example.com'), ['104.20.23.154'])
        self.assertEqual(len(seen), 1)
        self.assertEqual(str(seen[0].url), 'https://dns.google/resolve?name=example.com&type=A')
        for name in ('Authorization', 'Proxy-Authorization', 'Cookie'):
            self.assertNotIn(name, seen[0].headers)

    async def test_doh_oversize_and_redirect_and_failure_are_not_retried(self):
        original_client = httpx.AsyncClient
        for status, body in [(200, b'x' * 65_537), (302, b''), (503, b'')]:
            seen = []
            async def respond(request):
                seen.append(request)
                return httpx.Response(status, headers={'location':'https://other.invalid/'},
                                      stream=httpx.ByteStream(body))
            def client(**kwargs):
                return original_client(transport=httpx.MockTransport(respond), **kwargs)
            with self.subTest(status=status), patch.object(self.gateway.httpx, 'AsyncClient', side_effect=client):
                with self.assertRaises(ValueError):
                    await self.gateway._doh_addresses('example.com')
                self.assertEqual(len(seen), 1)

    async def test_doh_parser_rejects_nonpublic_mixed_and_invalid_answers(self):
        for raw in [self.doh_body(('104.20.23.154', '127.0.0.1')), self.doh_body(('198.18.1.1',)),
                    self.doh_body(('224.0.0.1',)), self.doh_body(()), self.doh_body(Status=3),
                    self.doh_body(TC=True), self.doh_body(Question=[{'name':'other.invalid.', 'type':1}]),
                    self.doh_body(('::1',)), b'[]', b'not-json']:
            with self.subTest(raw=raw[:100]), self.assertRaises(ValueError):
                self.gateway._parse_doh_result(raw, 'example.com')

    async def test_system_dns_failure_does_not_trigger_doh(self):
        with patch.object(self.loop, 'getaddrinfo', side_effect=socket.gaierror('lookup failed')), \
                patch.object(self.gateway, '_doh_addresses', new=AsyncMock()) as doh:
            with self.assertRaises(RuntimeError):
                await self.gateway._resolve_destination(self.run, 'https://example.com/')
            doh.assert_not_awaited()

    async def test_dns_timeout_cancels_pending_operation(self):
        closed = asyncio.Event()
        async def slow(*args, **kwargs):
            try:
                await asyncio.sleep(30)
            finally:
                closed.set()
        with patch.object(self.loop, 'getaddrinfo', side_effect=slow), \
                patch.object(self.gateway, 'DNS_TIMEOUT_SECONDS', .05):
            with self.assertRaises(TimeoutError):
                await self.gateway._resolve_destination(self.run, 'https://example.com/')
        self.assertTrue(closed.is_set())

    async def test_trailing_dot_pins_both_native_and_canonical_hostname(self):
        with patch.object(self.loop, 'getaddrinfo', new=AsyncMock(return_value=self.answers(['93.184.215.14']))):
            host, addresses = await self.gateway._resolve_destination(self.run, 'https://example.com./')
        self.assertEqual(host, 'example.com.')
        self.assertEqual(addresses, ['93.184.215.14'])

    async def test_literal_public_ip_needs_neither_system_dns_nor_doh(self):
        with patch.object(self.loop, 'getaddrinfo', new=AsyncMock()) as system, \
                patch.object(self.gateway, '_doh_addresses', new=AsyncMock()) as doh:
            self.assertEqual(await self.gateway._resolve_destination(self.run, 'https://1.1.1.1/'), ('', []))
            system.assert_not_awaited()
            doh.assert_not_awaited()


class NativeHelperTests(unittest.TestCase):
    bundle = Path(__file__).resolve().parents[1] / "integrations" / "deepseek-harness"

    def test_python_helper_full_sdk_no_model_calls_and_local_ipc_only(self):
        sdk = Mock()
        sdk.return_value.__enter__ = Mock(return_value=SimpleNamespace())
        sdk.return_value.__exit__ = Mock(return_value=False)
        response = Mock()
        response.read.return_value = json.dumps(RESULT).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        output = io.StringIO()
        with patch.dict(sys.modules, {"deepseek_harness": SimpleNamespace(DeepSeekHarness=sdk)}):
            helper = runpy.run_path(str(self.bundle / "fetch.py"))
        with patch("importlib.metadata.version", return_value="0.2.1rc2") as versions, \
                patch("sys.stdin", io.TextIOWrapper(io.BytesIO(b'{"url":"https://example.com/"}\n'))), \
                patch("sys.stdout", output), patch.object(Path, "write_text") as write_patch, \
                patch("urllib.request.build_opener", return_value=opener), \
                patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.invalid", "https_proxy": "http://proxy.invalid"}):
            helper['main']()
            self.assertNotIn("HTTP_PROXY", os.environ)
            self.assertNotIn("https_proxy", os.environ)
        self.assertEqual(json.loads(output.getvalue()), RESULT)
        self.assertEqual(versions.call_count, 2)
        settings = sdk.call_args.kwargs
        self.assertEqual(settings['profile'], 'sdk')
        self.assertEqual(settings['base_url'], 'https://api.deepseek.com')
        self.assertFalse({'provider', 'model', 'api_key'} & set(settings))
        self.assertEqual(sdk.return_value.method_calls, [])
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, 'http://127.0.0.1:8094/fetch')
        self.assertEqual(json.loads(req.data), {'url': 'https://example.com/'})
        response.read.assert_called_once_with(2_000_001)
        config = json.loads(write_patch.call_args.args[0])
        self.assertEqual(config[0], {'id': 'web', 'config': {'fetchProvider': 'http'}})
        self.assertEqual(config[1]['config']['maxResponseBytes'], 1_000_000)

    @unittest.skipUnless(shutil.which('node'), 'Node needed for plugin protocol test')
    def test_javascript_plugin_native_seam_one_attempt_limits_and_disposal(self):
        # Stub only local IPC and the native service. No HTTP or model requests.
        script = r'''
import assert from 'node:assert/strict';
import http from 'node:http';
import {EventEmitter} from 'node:events';
let handle, dispose, calls, closeCount;
http.createServer = fn => {
  handle = fn;
  return {listen(port, host) { assert.equal(port, 8094); assert.equal(host, '127.0.0.1'); },
    close(done) { closeCount++; done(); }, closeAllConnections() { closeCount++; }};
};
const {apply} = await import(process.argv[1]);
class Response extends EventEmitter {
  status = 200;
  writableFinished = false;
  writeHead(status) { this.status = status; return this; }
  setHeader() {}
  end(body) { this.body = body; this.writableFinished = true; return this; }
}
const request = raw => ({method: 'POST', url: '/fetch',
  async *[Symbol.asyncIterator]() { yield Buffer.from(raw); }});
function setup(fetch) {
  calls = 0; closeCount = 0;
  apply({web: {async fetch(body, signal) { calls++; return fetch(body, signal); }},
    on(event, fn) { assert.equal(event, 'dispose'); dispose = fn; }});
}
const result = {url:'https://example.com/',statusCode:404,body:{kind:'text',content:'missing'},truncated:false};
setup((body, signal) => { assert.deepEqual(body, {url:'https://example.com/'});
  assert.ok(signal instanceof AbortSignal); return result; });
const first = new Response(); await handle(request('{"url":"https://example.com/"}'), first);
assert.deepEqual(JSON.parse(first.body), result); assert.equal(calls, 1);
const second = new Response(); await handle(request('{"url":"https://example.com/"}'), second);
assert.equal(second.status, 409); assert.equal(calls, 1); await dispose();
for (const raw of ['{"url":"https://example.com/","headers":{}}', '[]', 'x'.repeat(8193),
  JSON.stringify({url:'https://example.com/'+'界'.repeat(3000)})]) {
  setup(() => { throw new Error('must not fetch'); });
  const res = new Response(); await handle(request(raw), res);
  assert.equal(res.status, 502); assert.equal(calls, 0); await dispose();
}
setup(() => { const error = new Error('secret log'); error.code='WEB_BLOCKED_URL'; throw error; });
const blocked = new Response(); await handle(request('{"url":"http://127.0.0.1"}'), blocked);
assert.deepEqual(JSON.parse(blocked.body), {code:'WEB_BLOCKED_URL'}); await dispose();
setup(() => ({...result,body:{kind:'text',content:'x'.repeat(2_000_000)}}));
const tooLarge = new Response(); await handle(request('{"url":"https://example.com/"}'), tooLarge);
assert.equal(tooLarge.status, 502); await dispose();
let entered;
const ready = new Promise(resolve => {entered = resolve;});
setup((body, signal) => new Promise((resolve, reject) => {
  signal.addEventListener('abort', () => reject(new Error('aborted')), {once:true}); entered();
}));
const pending = handle(request('{"url":"https://example.com/"}'), new Response());
await ready; await dispose(); await pending;
assert.equal(closeCount, 2);
console.log('native fetch seam, bounded IPC, one attempt and abort/dispose passed');
'''
        completed = subprocess.run(['node', '--input-type=module', '-e', script,
                                    (self.bundle / 'fetch.mjs').as_uri()],
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
