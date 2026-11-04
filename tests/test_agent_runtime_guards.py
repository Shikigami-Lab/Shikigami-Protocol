"""Phase-one guard regressions: synthetic credentials, temp state, no live effects."""
import asyncio
from copy import deepcopy
import json
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

import httpx

from src.agent.model_gateway import ModelGateway
from src.agent.runtime import AgentRuntime
from src.api.agent import _public_run


FAKE_KEY = "synthetic-host-credential-for-tests-only"
PRESET = {"base_url": "https://fixture.invalid/v1", "model": "fixture", "api_key": FAKE_KEY}
MESSAGES = [{"role": "system", "content": "Synthetic fixture"},
            {"role": "user", "content": "Synthetic timer request"}]


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class ModelGatewayGuardTests(unittest.IsolatedAsyncioTestCase):
    async def relay(self, status, chunks, *, key=FAKE_KEY):
        wire = []
        requests = []

        async def provider(request):
            requests.append(request)
            return httpx.Response(status, headers={"content-type": "text/event-stream"},
                                  stream=ChunkStream(chunks))

        async def respond(stage, **fields):
            wire.append((stage, fields))

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            gateway = ModelGateway({**PRESET, "api_key": key}, 1,
                                   authorize=lambda: None, client=client)
            await gateway.relay({"method": "POST", "path": "/v1/chat/completions",
                                 "body": {"messages": MESSAGES, "stream": True}}, respond)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers.get("authorization"), "Bearer " + key if key else None)
        self.assertEqual([stage for stage, _ in wire].count("start"), 1)
        self.assertEqual(wire[0][1]["status"], status)
        self.assertEqual(wire[-1][0], "end")
        self.assertEqual([stage for stage, _ in wire].count("end"), 1)
        return b"".join(fields["data"] for stage, fields in wire if stage == "chunk")

    async def test_non_success_bodies_never_cross_the_worker_boundary(self):
        raw = json.dumps({"error": {"message": "provider-private-diagnostic Bearer " + FAKE_KEY}}).encode()
        for status in (301, 401, 403, 429, 500):
            with self.subTest(status=status):
                body = await self.relay(status, [raw[:19], raw[19:]])
                self.assertNotIn(FAKE_KEY.encode(), body)
                self.assertNotIn(b"provider-private-diagnostic", body)
                self.assertNotIn(b"Bearer", body)
                self.assertIn(str(status), json.loads(body)["error"]["message"])

    async def test_success_stream_scrubs_secret_at_every_chunk_split(self):
        secret = FAKE_KEY.encode()
        prefix, suffix = b'data: {"text":"', b'"}\n\ndata: [DONE]\n\n'
        raw = prefix + secret + b" / " + secret + suffix
        expected = raw.replace(secret, b"[redacted]")
        for split in range(1, len(secret)):
            with self.subTest(split=split):
                body = await self.relay(200, [prefix + secret[:split],
                    secret[split:] + b" / " + secret[:split], secret[split:] + suffix])
                self.assertEqual(body, expected)
        with self.subTest(split="one-byte-chunks"):
            self.assertEqual(await self.relay(200, [raw[i:i + 1] for i in range(len(raw))]), expected)

    async def test_success_stream_preserves_normal_text_and_final_short_tail(self):
        raw = 'data: {"text":"合成回复"}\n\ndata: [DONE]\n\n'.encode()
        for key in (FAKE_KEY, ""):
            with self.subTest(has_key=bool(key)):
                self.assertEqual(await self.relay(200, [raw[:9], raw[9:-3], raw[-3:]], key=key), raw)


class AgentRuntimeGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shikigami-runtime-guards-")
        self.addCleanup(self.tmp.cleanup)
        # Nothing in these tests may launch Docker or deliver broadcasts.
        for target, replacement in (
            ("src.agent.runtime.asyncio.create_subprocess_exec", AsyncMock(side_effect=AssertionError("No subprocesses"))),
            ("src.agent.runtime.subprocess.run", Mock(side_effect=AssertionError("No subprocesses"))),
            ("src.core.broadcast.broadcast.push", AsyncMock()),
        ):
            guard = patch(target, replacement)
            guard.start()
            self.addCleanup(guard.stop)
        self.timers = Mock(spec=["start_timer", "list_timers", "stop_timer"])
        self.timers.start_timer.return_value = "synthetic-timer-id"
        self.timers.list_timers.return_value = []
        self.config = SimpleNamespace(agent={"enabled": True}, get_active_llm_preset=lambda: dict(PRESET))
        self.app = SimpleNamespace(state=SimpleNamespace(config=self.config))
        self.runtime = self.make_runtime()
        self.session = SimpleNamespace(id="alpha", profile_id="alpha")

    def make_runtime(self):
        return AgentRuntime(self.app, root=self.tmp.name, timer_manager=self.timers,
                            profile_loader=lambda _: {"tool_configs": {"timer": {"enabled": True}}})

    def prepare(self):
        return self.runtime.prepare(self.session, deepcopy(MESSAGES), dict(PRESET))

    async def asyncTearDown(self):
        await self.runtime.close()

    async def test_prepare_after_invalidate_uses_new_native_identity_and_stable_timer_session(self):
        old = self.prepare()
        await self.runtime.invalidate(self.session.profile_id)
        # Exercise durable revision state, not just the current instance's flags.
        await self.runtime.close()
        self.runtime = self.make_runtime()
        new = self.prepare()
        row = self.runtime.store.get_run(new.id)
        self.assertNotEqual(new.id, old.id)
        self.assertEqual(new.revision, old.revision + 1)
        self.assertEqual(row["session_id"], new.id)
        self.assertEqual(new.session_id, self.session.id)
        self.assertTrue(self.runtime.store.get_run(old.id)["stale"])
        self.assertTrue(self.runtime.store.is_current(new.id, self.session.profile_id))
        result = await self.runtime.execute_timer(new.id, "timer_create",
            {"seconds": 60, "label": "synthetic"}, "fresh-call")
        self.assertTrue(result["ok"])
        self.timers.start_timer.assert_called_once_with(60, "synthetic", self.session.id)
        with self.assertRaises(ValueError):
            await self.runtime.execute_timer(old.id, "timer_create",
                {"seconds": 60, "label": "stale"}, "stale-call")
        self.assertEqual(self.timers.start_timer.call_count, 1)

    async def test_prepare_rejects_non_profile_session_without_reserving_a_run(self):
        with self.assertRaises(ValueError):
            self.runtime.prepare(SimpleNamespace(id="group", profile_id="alpha"), MESSAGES, PRESET)
        self.assertEqual(self.runtime.store.list_runs("alpha"), [])
        self.assertEqual(self.runtime._runs, {})

    async def test_invalidate_purges_all_homes_beyond_display_limit_and_preserves_other_profile(self):
        homes = []
        for _ in range(103):
            run_id = str(uuid.uuid4())
            self.runtime.store.begin_run(run_id, "alpha", run_id)
            self.runtime.store.finish_run(run_id, "completed")
            home = self.runtime.data_root / "harness" / run_id
            home.mkdir(parents=True)
            (home / "synthetic-transcript.jsonl").write_text('{"fixture":true}\n', encoding="utf-8")
            homes.append(home)
        other_id = str(uuid.uuid4())
        self.runtime.store.begin_run(other_id, "beta", other_id)
        self.runtime.store.finish_run(other_id, "completed")
        other_home = self.runtime.data_root / "harness" / other_id
        other_home.mkdir()
        sentinel = other_home / "keep.txt"
        sentinel.write_text("synthetic other profile", encoding="utf-8")
        await self.runtime.invalidate("alpha")
        self.assertEqual([home.name for home in homes if home.exists()], [])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "synthetic other profile")
        self.assertTrue(self.runtime.store.is_current(other_id, "beta"))
        await self.runtime.invalidate("alpha")
        self.assertTrue(sentinel.is_file())

    def pause_host_effect(self):
        """Pause inside the real host transaction; only TimerManager is fake."""
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()

        def start_timer(*args):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise AssertionError("Test did not release paused timer effect")
            return "synthetic-paused-timer"

        self.timers.start_timer.side_effect = start_timer
        return entered, release

    def observe_stop_barrier(self):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        original = self.runtime._mark_stopped

        def stop(run):
            loop.call_soon_threadsafe(entered.set)
            return original(run)

        return entered, patch.object(self.runtime, "_mark_stopped", side_effect=stop)

    async def test_cancel_ack_waits_for_paused_host_effect_then_denies_later_effects(self):
        run = self.prepare()
        effect_entered, release = self.pause_host_effect()
        barrier_entered, barrier_patch = self.observe_stop_barrier()
        effect = asyncio.create_task(self.runtime.execute_timer(run.id, "timer_create",
            {"seconds": 60, "label": "synthetic"}, "in-flight"))
        cancel = None
        try:
            await asyncio.wait_for(effect_entered.wait(), 3)
            with barrier_patch:
                cancel = asyncio.create_task(self.runtime.cancel(run.id, "alpha"))
                await asyncio.wait_for(barrier_entered.wait(), 3)
                done, _ = await asyncio.wait({cancel}, timeout=0.05)
                self.assertFalse(done, "Cancellation acknowledged an unfinished host effect")
                self.assertFalse(run.terminal)
                self.assertEqual(self.runtime.store.get_run(run.id)["state"], "running")
                release.set()
                self.assertTrue((await asyncio.wait_for(effect, 3))["ok"])
                self.assertTrue(await asyncio.wait_for(cancel, 3))
            self.assertTrue(run.stopped.is_set())
            self.assertEqual(self.runtime.store.get_run(run.id)["state"], "cancelled")
            with self.assertRaises(ValueError):
                await self.runtime.execute_timer(run.id, "timer_create",
                    {"seconds": 60, "label": "late"}, "after-ack")
            self.timers.start_timer.assert_called_once_with(60, "synthetic", "alpha")
        finally:
            release.set()
            await asyncio.gather(effect, *([cancel] if cancel else []), return_exceptions=True)

    async def test_expired_task_records_timeout_through_sdk_cleanup(self):
        run = self.prepare()
        run.started_at -= run.limits['timeout_seconds'] + 1
        process = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0),
                                  stderr=SimpleNamespace(read=AsyncMock(return_value=b'')))
        with patch.object(self.runtime, 'status', new=AsyncMock(return_value={'available': True})), \
                patch.object(self.runtime, '_command', return_value=['synthetic']), \
                patch.object(self.runtime, '_send', new=AsyncMock()), \
                patch.object(self.runtime, '_remove_container'), \
                patch('src.agent.runtime.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)):
            await self.runtime._drive(run)
        row = self.runtime.store.get_run(run.id)
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['error'], 'Task timed out after 120 seconds')

    async def test_denied_model_request_records_the_budget_failure(self):
        run = self.prepare()
        run.gateway.requests = run.gateway.max_requests
        replies = []
        sent = asyncio.Event()
        input_frames = iter([
            {'kind': 'model_request', 'id': 'blocked-model-request'},
            {'kind': 'event', 'event': {'type': 'failed'}},
        ])

        async def readline():
            item = next(input_frames, None)
            if not item:
                return b''
            if item['kind'] == 'event':
                await sent.wait()
            return json.dumps(item).encode() + b'\n'

        async def send(item, message):
            replies.append(message)
            if message.get('kind') == 'response_end':
                sent.set()

        process = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0),
                                  stdout=SimpleNamespace(readline=readline),
                                  stderr=SimpleNamespace(read=AsyncMock(return_value=b'')))
        with patch.object(self.runtime, 'status', new=AsyncMock(return_value={'available': True})), \
                patch.object(self.runtime, '_command', return_value=['synthetic']), \
                patch.object(self.runtime, '_send', side_effect=send), \
                patch.object(self.runtime, '_remove_container'), \
                patch('src.agent.runtime.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)):
            await asyncio.wait_for(self.runtime._drive(run), 10)
        row = self.runtime.store.get_run(run.id)
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['error'], f'Model request limit reached ({run.gateway.max_requests} requests)')
        self.assertTrue(any(reply.get('status') == 409 for reply in replies))

    async def test_drive_failure_waits_for_host_effect_and_process_cleanup_before_terminal(self):
        run = self.prepare()
        effect_entered, release = self.pause_host_effect()
        barrier_entered, barrier_patch = self.observe_stop_barrier()
        process_waiting, process_release = asyncio.Event(), asyncio.Event()
        process = SimpleNamespace(returncode=None)

        async def wait_process():
            process_waiting.set()
            await process_release.wait()
            process.returncode = 0
            return 0

        process.wait = AsyncMock(side_effect=wait_process)
        run.process = process
        effect = asyncio.create_task(self.runtime.execute_timer(run.id, "timer_create",
            {"seconds": 60, "label": "synthetic"}, "in-flight"))
        try:
            await asyncio.wait_for(effect_entered.wait(), 3)
            with barrier_patch, patch.object(self.runtime, "status", AsyncMock(
                    return_value={"available": False, "detail": "Synthetic startup failure"})), \
                    patch.object(self.runtime, '_remove_container') as remove_container, \
                    patch("src.agent.mcp_server.create_timer_mcp_app", return_value=SimpleNamespace()):
                run.task = asyncio.create_task(self.runtime._drive(run))
                await asyncio.wait_for(barrier_entered.wait(), 3)
                await asyncio.wait({run.task}, timeout=0.05)
                self.assertFalse(run.task.done())
                self.assertFalse(run.terminal)
                self.assertFalse(process_waiting.is_set(), "Cleanup bypassed an unfinished host effect")
                release.set()
                await asyncio.wait_for(effect, 3)
                await asyncio.wait_for(process_waiting.wait(), 3)
                self.assertFalse(run.terminal, "Terminal published before process exit")
                self.assertEqual(self.runtime.store.get_run(run.id)["state"], "running")
                process_release.set()
                await asyncio.wait_for(run.task, 3)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(self.runtime.store.get_run(run.id)["state"], "failed")
            events = []
            while not run.queue.empty():
                events.append(run.queue.get_nowait())
            self.assertIsNone(events[-1])
            terminal = [item["agent_event"]["type"] for item in events if item
                        and item.get("agent_event", {}).get("type") in ("completed", "cancelled", "failed")]
            self.assertEqual(terminal, ["failed"])
        finally:
            release.set()
            process_release.set()
            await asyncio.gather(effect, *([run.task] if run.task else []), return_exceptions=True)


class PublicRunMappingTests(unittest.TestCase):
    def test_public_run_maps_native_session_to_stable_profile_without_mutating_storage(self):
        native_id = str(uuid.uuid4())
        row = {"run_id": native_id, "profile_id": "alpha", "session_id": native_id,
               "state": "completed", "events": [
                   {"seq": 1, "type": "tool_call", "result": {"arguments": {"seconds": 60, "label": "synthetic"}}},
                   {"seq": 2, "type": "completed"}]}
        original = deepcopy(row)
        public = _public_run(None, row)
        self.assertEqual(public["session_id"], "alpha")
        self.assertEqual(public["run_id"], native_id)
        self.assertEqual(public["request_id"], native_id)
        self.assertEqual(public["id"], native_id)
        for event in public["events"]:
            self.assertEqual(event["session_id"], "alpha")
            self.assertEqual(event["profile_id"], "alpha")
            self.assertEqual(event["run_id"], native_id)
            self.assertEqual(event["sequence"], event["seq"])
        self.assertEqual(json.loads(public["events"][0]["arguments"]), {"seconds": 60, "label": "synthetic"})
        self.assertEqual(row, original)


if __name__ == "__main__":
    unittest.main()
