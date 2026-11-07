"""Exercise the real rc2 SDK/runtime against an offline OpenAI SSE fixture.

Run only inside the documented network-disabled disposable container. No app
configuration, real model, character card, database or host port is needed.
"""
import importlib.metadata
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from deepseek_harness import DeepSeekHarness

sys.path.insert(0, '/opt/app')
from src.agent.context import build_turn_context
from src.llm.message_normalization import PromptMessage


TOKEN = secrets.token_hex(24)
WIRES = []
RELEASE_SLOW = threading.Event()
CHECKS = []


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        WIRES.append(payload)
        if len(WIRES) > 30:
            self.send_error(429, 'offline fixture request cap'); return
        text = json.dumps(payload['messages'], ensure_ascii=False)
        if 'case:error' in text:
            self.send_error(400, 'intentional fixture error'); return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()

        def delta(value, finish=None):
            obj = {'id': 'fixture-response', 'object': 'chat.completion.chunk',
                   'created': 1, 'model': 'fixture-model', 'choices': [
                       {'index': 0, 'delta': value, 'finish_reason': finish}]}
            self.wfile.write(('data: ' + json.dumps(obj) + '\n\n').encode())
            self.wfile.flush()

        try:
            has_result = any(m['role'] == 'tool' for m in payload['messages'])
            if 'case:read' in text and not has_result:
                delta({'role': 'assistant', 'tool_calls': [{
                    'index': 0, 'id': 'fixture-read', 'type': 'function', 'function': {
                        'name': 'read', 'arguments': json.dumps({'file_path': '/workspace/fixtures/readme.txt'})}}]})
                delta({}, 'tool_calls')
            else:
                delta({'role': 'assistant', 'content': 'READY '})
                if 'case:slow' in text:
                    RELEASE_SLOW.wait(10)
                delta({'content': 'DONE'})
                delta({}, 'stop')
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def control(path, body=None, *, token=TOKEN, expected=200, with_status=False):
    req = urllib.request.Request('http://127.0.0.1:8091' + path,
        data=json.dumps(body or {}).encode(), headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            status, data = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, data = error.code, error.read()
    assert status in ((expected,) if isinstance(expected, int) else expected), (path, status, data[:1200])
    result = json.loads(data) if data else {}
    return (status, result) if with_status else result


def wait_event(binding, kinds, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = control('/inspect')
        matches = [e for e in state['events'] if e.get('request_id') == binding['request_id'] and e['type'] in kinds]
        if matches:
            return matches[-1]
        time.sleep(.03)
    raise AssertionError(('missing event', kinds, state['events'][-12:]))


def main():
    assert importlib.metadata.version('deepseek-harness-sdk') == '0.2.1rc2'
    assert importlib.metadata.version('deepseek-harness-runtime-bin') == '0.2.1rc2'
    Path('/workspace/fixtures').mkdir()
    Path('/workspace/fixtures/readme.txt').write_text('SYNTHETIC_READ_RESULT_42', encoding='utf-8')
    server = ThreadingHTTPServer(('127.0.0.1', 8090), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with DeepSeekHarness(dsh_home='/workspace/harness-home', cwd='/workspace', profile='sdk',
                provider='fixture', model='fixture-model', patches=('/opt/contract/fixture.patch.yml',),
                env={'FIXTURE_KEY': 'offline-fixture', 'CONTRACT_TOKEN': TOKEN},
                max_tokens=128, initialize_timeout_seconds=60, request_timeout_seconds=15) as harness:
            harness.start()

            def start(name, case='plain', origin='user', revision=0, profile=None, submit=True, history=()):
                sid = 'fixture-' + name
                persona = 'Character fixture {{literal}}. Persona for: ' + (profile or name) + '.\n'
                body = build_turn_context(profile_id=profile or name, request_id=name,
                    context_revision=revision, origin=origin, messages=[
                        PromptMessage({'role': 'system', 'content': persona + 'TIME_SLOT'},
                            time_index_span=(len(persona), len(persona) + 9)),
                        *[PromptMessage({'role': role, 'content': content}, time_sources=(stamp,))
                          for role, content, stamp in history],
                        PromptMessage({'role': 'user', 'content': 'case:' + case + ' run:' + name},
                            time_sources=('2026-10-30 09:00',))])
                body['session_id'] = sid
                if submit:
                    control('/start', body)
                return body

            catalog = control('/inspect')['tools']
            assert {'read', 'write', 'bash', 'subagent', 'skill'}.issubset(catalog)
            control('/inspect', token='wrong-token', expected=403)
            plain = start('plain')
            wait_event(plain, {'completed'})
            state = control('/inspect')
            deltas = [e['text'] for e in state['events'] if e.get('request_id') == 'plain' and e['type'] == 'reply_delta']
            assert ''.join(deltas) == 'READY DONE', deltas
            first = WIRES[0]
            systems = [m for m in first['messages'] if m['role'] == 'system']
            assert len(systems) == 1 and '{{literal}}' in systems[0]['content']
            assert '1. user：片段1：2026-10-30 09:00' in systems[0]['content']
            assert first['messages'][1]['content'] == 'case:plain run:plain'
            assert [t['function']['name'] for t in first['tools']] == ['read']
            CHECKS.append('real full SDK, scoped persona, literal braces, reply streaming, restricted native tool schema')

            reflection = start('reflection', origin='reflection')
            wait_event(reflection, {'completed'})
            sources = [e['data']['source']['kind'] for e in control('/inspect')['audit']
                       if e['session_id'] == reflection['session_id'] and e['type'] == 'user/message']
            assert 'shikigami-reflection' in sources and 'user' not in sources, sources
            CHECKS.append('reflection native source remains distinct from human input')

            proactive = start('proactive', origin='proactive')
            wait_event(proactive, {'completed'})
            sources = [e['data']['source']['kind'] for e in control('/inspect')['audit']
                       if e['session_id'] == proactive['session_id'] and e['type'] == 'user/message']
            assert 'shikigami-proactive' in sources and 'user' not in sources, sources
            CHECKS.append('proactive research keeps its own native provenance')

            for decision in ('allowed-once', 'rejected', 'timeout'):
                run = start('read-' + decision, 'read')
                approval = wait_event(run, {'approval_requested'})
                before = len(WIRES)
                assert not control('/answer', {**run, 'approval_id': 'wrong', 'outcome': 'allowed-once'})['accepted']
                control('/answer', {**run, 'profile_id': 'wrong', 'approval_id': approval['approval_id'],
                                   'outcome': 'allowed-once'}, expected=409)
                assert len(WIRES) == before
                if decision != 'timeout':
                    answer = {**run, 'approval_id': approval['approval_id'], 'outcome': decision}
                    assert control('/answer', answer)['accepted']
                    assert not control('/answer', answer)['accepted']
                wait_event(run, {'completed'})
                wire = WIRES[-1]
                results = [m for m in wire['messages'] if m['role'] == 'tool']
                assert len(results) == 1, results
                assert ('SYNTHETIC_READ_RESULT_42' in json.dumps(results)) == (decision == 'allowed-once'), results
                audits = [e for e in control('/inspect')['audit'] if e['session_id'] == run['session_id']]
                assert sum(e['type'] == 'approval/asked' for e in audits) == 1
                assert sum(e['type'] == 'approval/decided' for e in audits) == 1
            CHECKS.append('native read continuation; allow/deny/timeout; wrong and duplicate answers fail closed')

            slow = start('slow', 'slow')
            wait_event(slow, {'reply_delta'})
            # This milestone has no queue policy: never inject a reflection
            # into a busy run under a different request identity.
            control('/start', {**slow, 'origin': 'reflection', 'request_id': 'busy-reflection'}, expected=409)
            other = start('parallel')
            assert control('/cancel', slow)['accepted']
            wait_event(slow, {'cancelled'})
            wait_event(other, {'completed'})
            slow_calls = sum('run:slow' in json.dumps(w['messages']) for w in WIRES)
            RELEASE_SLOW.set()
            time.sleep(.1)
            assert sum('run:slow' in json.dumps(w['messages']) for w in WIRES) == slow_calls
            CHECKS.append('cancel A with partial reply retained while B completes, no new A model calls')

            pending = start('cancel-approval', 'read')
            asked = wait_event(pending, {'approval_requested'})
            assert control('/cancel', pending)['accepted']
            wait_event(pending, {'cancelled'})
            assert not control('/answer', {**pending, 'approval_id': asked['approval_id'], 'outcome': 'allowed-once'})['accepted']
            CHECKS.append('cancel withdraws pending approval; late grant cannot execute')

            old = start('old-revision', 'read', profile='shared')
            asked = wait_event(old, {'approval_requested'})
            fresh = start('new-revision', profile='shared', revision=1)
            wait_event(old, {'cancelled'})
            wait_event(fresh, {'completed'})
            control('/answer', {**old, 'approval_id': asked['approval_id'], 'outcome': 'allowed-once'}, expected=409)
            control('/start', old, expected=409)
            # A retired session still contains native history. It must never
            # become eligible for another role/request merely because unbound.
            control('/start', {**old, 'context_revision': 1, 'request_id': 'reuse-old'}, expected=409)
            control('/start', {**old, 'profile_id': 'other-role', 'request_id': 'reuse-other-role'}, expected=409)
            CHECKS.append('revision invalidation closes old UI run, revokes answers and prohibits native session reuse')

            racing_old = start('race-old', 'read', profile='racing')
            wait_event(racing_old, {'approval_requested'})
            racing = start('race-new', profile='racing', revision=1, submit=False)
            with ThreadPoolExecutor(max_workers=2) as pool:
                submitted = [pool.submit(control, '/start', racing, expected=(200, 409), with_status=True) for _ in range(2)]
                assert sorted(f.result()[0] for f in submitted) == [200, 409]
            wait_event(racing_old, {'cancelled'})
            wait_event(racing, {'completed'})
            assert sum('run:race-new' in json.dumps(w['messages']) for w in WIRES) == 1
            CHECKS.append('overlapping duplicate starts during revision cancellation enqueue exactly one native run')

            prior_events = [e for e in control('/inspect')['events'] if e.get('request_id') == 'plain']
            after_completed = start('after-completed', profile='plain', revision=1)
            wait_event(after_completed, {'completed'})
            assert [e for e in control('/inspect')['events'] if e.get('request_id') == 'plain'] == prior_events
            CHECKS.append('invalidating a completed run preserves its terminal UI state')

            error = start('error', 'error')
            wait_event(error, {'failed'})
            CHECKS.append('model transport error is reported as failed, not completed')

            history_rows = [('user', 'HISTORY_USER_A', '2026-05-10 10:00'),
                            ('assistant', 'HISTORY_REPLY_A1', '2026-05-10 10:01'),
                            ('assistant', 'HISTORY_REPLY_A2', '2026-05-10 10:02'),
                            ('user', 'HISTORY_USER_B', '2026-05-11 11:00'),
                            ('assistant', 'HISTORY_REPLY_B', '2026-05-11 11:01')]
            before = len(WIRES)
            historic = start('history', 'read', profile='history-role', history=history_rows)
            approval = wait_event(historic, {'approval_requested'})
            assert control('/answer', {**historic, 'approval_id': approval['approval_id'], 'outcome': 'allowed-once'})['accepted']
            wait_event(historic, {'completed'})
            imported_wires = WIRES[before:]
            assert len(imported_wires) == 2, 'history initialization must make no extra model calls'
            for wire in imported_wires:
                assert wire['messages'][1:6] == historic['messages'][1:]
                assert '2. assistant：片段1：2026-05-10 10:01；片段2：2026-05-10 10:02' in wire['messages'][0]['content']
                assert '5. user：片段1：2026-10-30 09:00' in wire['messages'][0]['content']
                for marker in ('HISTORY_USER_A', 'HISTORY_REPLY_A1', 'HISTORY_REPLY_A2', 'HISTORY_USER_B', 'HISTORY_REPLY_B'):
                    assert json.dumps(wire['messages']).count(marker) == 1
            visible = [e for e in control('/inspect')['events'] if e.get('request_id') == 'history' and e['type'] == 'reply_delta']
            assert ''.join(e['text'] for e in visible) == 'READY DONE', 'seeded replies must not replay as UI output'
            control('/start', historic, expected=409)
            CHECKS.append('normalized multi-turn history keeps roles/text/time through native read continuation without duplicates or extra model calls')

            reopened = control('/roundtrip', historic)
            assert reopened['before'] == reopened['after'], 'native persisted history must reopen exactly'
            imported_assistants = [m for m in reopened['after'] if m['role'] == 'assistant' and any(
                'HISTORY_REPLY' in b.get('text', '') for b in m['content'])]
            assert len(imported_assistants) == 2
            assert all(m['source'] == {'kind': 'model', 'provider': 'shikigami-history/unknown-provider',
                                      'model': 'unknown-model'} for m in imported_assistants)
            CHECKS.append('native JSONL close/reopen preserves imported history with explicit unknown model attribution')

            sanitized = start('history-deleted', profile='history-role', revision=1, history=history_rows[3:])
            wait_event(sanitized, {'completed'})
            cleaned = json.dumps(WIRES[-1]['messages'])
            assert 'HISTORY_USER_A' not in cleaned and 'HISTORY_REPLY_A' not in cleaned
            assert 'HISTORY_REPLY_B' in cleaned
            control('/roundtrip', historic, expected=409)
            other_role = start('separate-history-role')
            wait_event(other_role, {'completed'})
            assert 'HISTORY_' not in json.dumps(WIRES[-1]['messages'])
            assert 'Persona for: separate-history-role.' in WIRES[-1]['messages'][0]['content']
            assert 'Persona for: history-role.' not in WIRES[-1]['messages'][0]['content']
            CHECKS.append('new context revision omits removed history, refuses old restore control, and isolates other roles')

            leading = start('leading-assistant', history=[('assistant', 'LEADING_OLD_REPLY', '2026-05-01 08:00')])
            wait_event(leading, {'completed'})
            assert WIRES[-1]['messages'][1:4] == leading['messages'][1:]
            assert WIRES[-1]['messages'][1]['content'] == '（对话开始）'
            assert '2. assistant：片段1：2026-05-01 08:00' in WIRES[-1]['messages'][0]['content']
            CHECKS.append('history window starting with assistant keeps the existing normalizer placeholder and correct time index')

            for wire in WIRES:
                assert sum(m['role'] == 'system' for m in wire['messages']) == 1
                assert '{{literal}}' in wire['messages'][0]['content']
                assert [t['function']['name'] for t in wire.get('tools', [])] == ['read']
                assert wire['messages'][1]['role'] == 'user'
                assert wire['messages'][1]['content'].startswith(('case:', 'HISTORY_', '（对话开始）'))
            CHECKS.append('every captured model step preserves a single system and the indexed current-message position')

            print(json.dumps({'status': 'protocol-passed', 'sdk': '0.2.1rc2', 'profile': 'sdk', 'model': 'offline fixture',
                'model_requests': len(WIRES), 'checks': CHECKS, 'catalog': catalog,
                'first_wire_roles': [m['role'] for m in first['messages']],
                'remaining': ['App /chat, reflection scheduling, production transport, UI and live providers are not connected.',
                              'Revision authorization is in-memory in this fixture; durable app deletion/restart enforcement is not implemented.']}, ensure_ascii=False))
    finally:
        RELEASE_SLOW.set()
        server.shutdown()


if __name__ == '__main__':
    main()
