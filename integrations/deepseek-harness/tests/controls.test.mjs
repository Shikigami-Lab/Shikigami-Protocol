import test from 'node:test';
import assert from 'node:assert/strict';

// Only the native event boundary is doubled here; the real SDK owns execution.
const module = await import('../controls.mjs').catch(error => {
  if (error.code === 'ERR_MODULE_NOT_FOUND') return {};
  throw error;
});
const binding = (session_id = 'A', request_id = 'run-A') => Object.freeze({
  profile_id: 'profile', request_id, context_revision: 7, session_id,
});
function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}
function fixture(t, options = {}) {
  assert.equal(typeof module.installControls, 'function', 'installControls must be implemented');
  const listeners = new Map(), agents = new Map(), owners = new Map(), events = [];
  const ctx = {
    agents: { get: id => agents.get(id) },
    on(name, fn, opts) {
      const list = listeners.get(name) ?? [];
      opts?.prepend ? list.unshift(fn) : list.push(fn);
      listeners.set(name, list);
      return () => { const i = list.indexOf(fn); if (i < 0) return false; list.splice(i, 1); return true; };
    },
  };
  const emit = (name, ...args) => { for (const fn of [...(listeners.get(name) ?? [])]) fn(...args); };
  const waterfall = async (name, arg, end = async () => ({ kind: 'allow' })) => {
    const list = [...(listeners.get(name) ?? [])];
    const dispatch = i => i === list.length ? end() : list[i](arg, () => dispatch(i + 1));
    return dispatch(0);
  };
  const controls = module.installControls(ctx, {
    owner: agent => owners.get(agent),
    publish: event => { events.push(event); return options.publish?.(event); },
    approvalTimeoutMs: options.timeout ?? 50,
  });
  t.after(() => emit('dispose'));
  function agent(id = 'A') {
    const abort = new AbortController();
    const a = { id, session: { id }, abort, cancels: [],
      cancel(cause) { this.cancels.push(cause); abort.abort(cause); },
      async whenIdle() {},
    };
    agents.set(id, a); owners.set(a, binding(id, `run-${id}`));
    return a;
  }
  const a = agent();
  const session = (a, type, data, seq = 1) => emit('session/event', a.session, { type, data, seq });
  function start(a, turn = 1, kind = 'user') {
    session(a, 'turn/start', { turn });
    emit('agent/inbox/claimed', { agent: a, turn,
      message: { id: `m-${turn}`, role: 'user', content: [], source: { kind } } });
  }
  async function ask(a, callId = 'call', args = { file_path: '/workspace/fixtures/readme.txt' }) {
    const exec = { agent: a, name: 'read', callId, rootCallId: callId,
      token: Symbol(callId), arguments: args, signal: a.abort.signal };
    const gate = await waterfall('tools/pre-execute', exec);
    assert.equal(gate.kind, 'ask');
    const answer = waterfall('approval/request', { agent: a, toolName: 'read', callId,
      signal: a.abort.signal, reason: gate.reason }, async () => 'unavailable');
    const event = events.findLast(e => e.type === 'approval_requested' && e.call_id === callId);
    assert.ok(event, 'native answerer publishes approval');
    return { answer, event, exec };
  }
  start(a);
  return { ctx, emit, waterfall, controls, agents, owners, events, a, agent, session, start, ask };
}

test('every native read argument set asks once and publishes exact action/path/args', async t => {
  const f = fixture(t);
  for (const args of [{ file_path: '/workspace/fixtures/readme.txt' },
    { file_path: '/different/path', offset: 3, limit: 9, extra: 'kept' }]) {
    const { answer, event } = await f.ask(f.a, args.file_path, args);
    assert.equal(event.action, 'read'); assert.equal(event.path, args.file_path);
    assert.equal(event.tool_name, 'read'); assert.deepEqual(event.arguments, args);
    assert.ok(event.reason.includes(args.file_path));
    assert.deepEqual(Object.fromEntries(Object.keys(binding()).map(k => [k, event[k]])), binding());
    assert.equal(f.controls.answer(binding(), event.approval_id, 'allowed-once'), true);
    assert.equal(await answer, 'allowed-once');
    assert.equal(f.controls.answer(binding(), event.approval_id, 'allowed-once'), false);
  }
  assert.equal(f.events.filter(e => e.type === 'approval_requested').length, 2);
});

test('answer rejects wrong profile/run/revision/session/id/outcome without consuming request', async t => {
  const f = fixture(t), { answer, event } = await f.ask(f.a);
  for (const key of Object.keys(binding())) {
    assert.equal(f.controls.answer({ ...binding(), [key]: 'wrong' }, event.approval_id, 'allowed-once'), false);
  }
  assert.equal(f.controls.answer(binding(), 'wrong', 'allowed-once'), false);
  for (const outcome of ['allow', 'cancelled', 'unavailable', true, null]) {
    assert.equal(f.controls.answer(binding(), event.approval_id, outcome), false);
  }
  assert.equal(f.controls.answer({ ...binding(), approval_id: event.approval_id }, event.approval_id, 'rejected'), true);
  assert.equal(await answer, 'rejected');
});

test('unanswered request cannot resolve to allow and expires fail closed', async t => {
  const f = fixture(t, { timeout: 15 }), { answer, event } = await f.ask(f.a);
  let settled = false; answer.then(() => { settled = true; });
  await Promise.resolve(); assert.equal(settled, false);
  assert.equal(await answer, 'unavailable');
  assert.equal(f.controls.answer(binding(), event.approval_id, 'allowed-once'), false);
});

test('cancel waits for A quiescence, cancels pending approval, and leaves B untouched', async t => {
  const f = fixture(t), b = f.agent('B'); f.start(b);
  const pa = await f.ask(f.a, 'a'), pb = await f.ask(b, 'b');
  const idle = deferred(); f.a.whenIdle = () => idle.promise;
  const nativeCancel = f.a.cancel.bind(f.a);
  let noticeAtCancel;
  f.a.cancel = cause => {
    noticeAtCancel = f.events.findLast(e => e.session_id === 'A');
    nativeCancel(cause);
  };
  let acknowledged = false;
  const cancel = f.controls.cancel(binding()).then(value => { acknowledged = true; return value; });
  assert.equal(await pa.answer, 'cancelled');
  await Promise.resolve(); assert.equal(acknowledged, false);
  assert.equal(noticeAtCancel.type, 'status'); assert.equal(noticeAtCancel.status, 'cancelling');
  assert.equal(f.events.some(e => ['completed', 'failed', 'cancelled'].includes(e.type)), false);
  assert.deepEqual(f.a.cancels, [{ kind: 'user' }]); assert.deepEqual(b.cancels, []);
  assert.equal(f.controls.answer(binding('B', 'run-B'), pb.event.approval_id, 'allowed-once'), true);
  assert.equal(await pb.answer, 'allowed-once');
  idle.resolve(); assert.equal(await cancel, true);
  assert.equal(f.controls.answer(binding(), pa.event.approval_id, 'allowed-once'), false);
  assert.equal(f.events.some(e => ['completed', 'cancelled'].includes(e.type)), false);
});

test('cancel rejects stale bindings and registry replacement without touching agents', async t => {
  const f = fixture(t);
  assert.equal(await f.controls.cancel({ ...binding(), request_id: 'old' }), false);
  f.agents.set('A', { ...f.a });
  assert.equal(await f.controls.cancel(binding()), false);
  assert.deepEqual(f.a.cancels, []);
});

test('revoke resolves pending and prevents late approval and old-binding reads', async t => {
  const f = fixture(t), p = await f.ask(f.a);
  f.controls.revoke(f.a);
  assert.equal(await p.answer, 'cancelled');
  assert.equal(f.controls.answer(binding(), p.event.approval_id, 'allowed-once'), false);
  const gate = await f.waterfall('tools/pre-execute', p.exec);
  assert.ok(['deny', 'cancel'].includes(gate.kind));
});

test('binding replacement invalidates pending approval even before another event', async t => {
  const f = fixture(t), p = await f.ask(f.a);
  f.owners.set(f.a, binding('A', 'next-run'));
  assert.equal(f.controls.answer(binding(), p.event.approval_id, 'allowed-once'), false);
  assert.equal(await p.answer, 'cancelled');
});

test('missing ownership and uncorrelated read approvals fail closed; other tools delegate', async t => {
  const f = fixture(t);
  assert.deepEqual(await f.waterfall('tools/pre-execute', { name: 'write' }, async () => ({ kind: 'deny', reason: 'native' })),
    { kind: 'deny', reason: 'native' });
  assert.equal(await f.waterfall('approval/request', { agent: f.a, toolName: 'read', callId: 'no-gate' }, async () => 'allowed-once'), 'unavailable');
  f.owners.delete(f.a);
  assert.equal((await f.waterfall('tools/pre-execute', { name: 'read', agent: f.a, signal: f.a.abort.signal,
    arguments: {}, callId: 'no-owner' })).kind, 'deny');
});

test('native denial/cancellation preserved and abort during downstream gate never asks', async t => {
  const f = fixture(t), exec = { name: 'read', agent: f.a, signal: f.a.abort.signal,
    arguments: { file_path: 'x' }, callId: 'c' };
  const denial = { kind: 'deny', reason: 'existing policy' };
  assert.deepEqual(await f.waterfall('tools/pre-execute', exec, async () => denial), denial);
  assert.equal((await f.waterfall('tools/pre-execute', exec, async () => {
    f.a.abort.abort(); return { kind: 'allow' };
  })).kind, 'cancel');
  assert.equal(f.events.length, 0);
});

test('reflection stays non-human and waits for explicit trusted answer', async t => {
  const f = fixture(t); f.start(f.a, 2, 'project-reflection');
  const p = await f.ask(f.a);
  assert.equal(p.event.source.kind, 'project-reflection');
  assert.equal(f.controls.answer(binding(), p.event.approval_id, 'allow'), false);
  assert.equal(f.controls.answer(binding(), p.event.approval_id, 'rejected'), true);
  assert.equal(await p.answer, 'rejected');
});

test('stream projects text only, deduplicates chunks, and never emits success from status/end', t => {
  const f = fixture(t);
  const stream = frame => f.emit('agent/assistant-stream', { agent: f.a, frame });
  stream({ type: 'start', attemptId: 'A:1', revision: 1, turn: 1, step: 1 });
  const text = { type: 'chunk', attemptId: 'A:1', revision: 2, index: 0, time: 1,
    chunk: { type: 'text-delta', index: 0, text: 'hello' } };
  stream(text); stream(text);
  stream({ ...text, revision: 3, index: 1, chunk: { type: 'reasoning-delta', text: 'SECRET' } });
  stream({ ...text, revision: 4, index: 2, chunk: { type: 'block-end', block: { type: 'reasoning', text: 'SECRET' } } });
  stream({ ...text, revision: 5, index: 3, chunk: { type: 'tool-call-delta', argumentsDelta: 'SECRET' } });
  stream({ type: 'end', attemptId: 'A:1', revision: 6, index: 4, outcome: { kind: 'committed' } });
  f.emit('agent/status', { agent: f.a, status: 'idle' });
  assert.equal(f.events.filter(e => e.type === 'reply_delta').length, 1);
  assert.equal(f.events[0].text, 'hello');
  assert.equal(JSON.stringify(f.events).includes('SECRET'), false);
  assert.deepEqual(f.events.map(e => e.type), ['reply_delta', 'status']);
});

test('native durable tool calls/results become tool events, filtering reasoning and metadata', t => {
  const f = fixture(t);
  f.session(f.a, 'tool/call', { turn: 1, step: 1, callId: 'c', name: 'read',
    arguments: '{"file_path":"x"}' }, 4);
  f.session(f.a, 'tool/result', { turn: 1, step: 1, message: { isError: false,
    source: { kind: 'tool', callId: 'c' }, content: [{ type: 'text', text: 'file content' },
      { type: 'reasoning', text: 'SECRET' }] }, meta: { secret: 'SECRET' } }, 5);
  assert.deepEqual(f.events.map(e => e.type), ['tool_call', 'tool_result']);
  assert.equal(f.events[1].call_id, 'c');
  assert.deepEqual(f.events[1].content, [{ type: 'text', text: 'file content' }]);
  assert.equal(JSON.stringify(f.events).includes('SECRET'), false);
});

for (const [reason, type] of [[{ kind: 'completed' }, 'completed'],
  [{ kind: 'aborted', reason: { kind: 'user' } }, 'cancelled'],
  [{ kind: 'error', error: { code: 'X', message: 'failed' } }, 'failed'],
  [{ kind: 'blocked' }, 'failed'], [{ kind: 'max-tokens' }, 'failed'],
  [{ kind: 'interrupted' }, 'failed'], [{ kind: 'future-kind' }, 'failed']]) {
  test(`turn/end ${reason.kind} maps to ${type} with actual reason and no duplicates`, t => {
    const f = fixture(t);
    f.session(f.a, 'turn/end', { turn: 1, reason }, 8);
    f.session(f.a, 'turn/end', { turn: 1, reason }, 8);
    assert.deepEqual(f.events.map(e => e.type), [type]);
    assert.deepEqual(f.events[0].reason, reason);
  });
}

test('old turn/attempt cannot be relabelled onto a replacement run', t => {
  const f = fixture(t);
  f.emit('agent/assistant-stream', { agent: f.a,
    frame: { type: 'start', attemptId: 'A:1', revision: 1, turn: 1, step: 1 } });
  f.owners.set(f.a, binding('A', 'new-run')); f.start(f.a, 2, 'project-reflection');
  f.emit('agent/assistant-stream', { agent: f.a, frame: { type: 'chunk', attemptId: 'A:1',
    revision: 2, index: 0, chunk: { type: 'text-delta', text: 'old' } } });
  f.session(f.a, 'turn/end', { turn: 1, reason: { kind: 'completed' } }, 10);
  assert.equal(f.events.length, 0);
  f.session(f.a, 'turn/end', { turn: 2, reason: { kind: 'completed' } }, 12);
  assert.equal(f.events[0].request_id, 'new-run');
  assert.equal(f.events[0].source.kind, 'project-reflection');
});

test('dispose cancels pending and repeated native approval never issues a second prompt', async t => {
  const f = fixture(t), p = await f.ask(f.a);
  assert.equal(await f.waterfall('approval/request', { agent: f.a, toolName: 'read', callId: 'call',
    signal: f.a.abort.signal }, async () => 'allowed-once'), 'unavailable');
  assert.equal(f.events.filter(e => e.type === 'approval_requested').length, 1);
  f.emit('dispose'); assert.equal(await p.answer, 'cancelled');
});

test('publish failures close approval instead of leaving an executable grant', async t => {
  const f = fixture(t, { publish() { throw new Error('transport down'); } });
  const p = await f.ask(f.a);
  assert.equal(await p.answer, 'unavailable');
  assert.equal(f.controls.answer(binding(), p.event.approval_id, 'allowed-once'), false);
});

test('asynchronous publish rejection and agent disposal also close pending requests', async t => {
  const f = fixture(t, { publish: () => Promise.reject(new Error('offline')) });
  const p = await f.ask(f.a);
  assert.equal(await p.answer, 'unavailable');
  const g = fixture(t), q = await g.ask(g.a);
  g.emit('agent/disposed', { agent: g.a }); g.agents.delete(g.a.id);
  assert.equal(await q.answer, 'cancelled');
  assert.equal(g.controls.answer(binding(), q.event.approval_id, 'allowed-once'), false);
});

test('ownership changes across await cannot produce a native approval grant', async t => {
  const f = fixture(t), wait = deferred();
  const gate = f.waterfall('tools/pre-execute', { name: 'read', agent: f.a,
    signal: f.a.abort.signal, callId: 'c', arguments: { file_path: 'x' } }, () => wait.promise);
  f.owners.set(f.a, binding('A', 'new'));
  wait.resolve({ kind: 'allow' });
  assert.equal((await gate).kind, 'cancel');
  assert.equal(f.events.length, 0);
});

test('cancel acknowledgement cannot hide a failed native idle wait', async t => {
  const f = fixture(t);
  f.a.whenIdle = async () => { throw new Error('did not settle'); };
  await assert.rejects(f.controls.cancel(binding()), /did not settle/);
  assert.equal(f.events.some(e => ['completed', 'cancelled'].includes(e.type)), false);
});

test('unattributed input is unknown, not user; public payloads cannot mutate approval args', async t => {
  const f = fixture(t);
  f.owners.set(f.a, binding('A', 'unknown-source'));
  f.session(f.a, 'turn/start', { turn: 2 });
  const p = await f.ask(f.a);
  assert.deepEqual(p.event.source, { kind: 'unknown' });
  p.event.arguments.file_path = 'tampered';
  assert.equal(p.exec.arguments.file_path, '/workspace/fixtures/readme.txt');
  assert.equal(f.controls.answer(binding('A', 'unknown-source'), p.event.approval_id, 'rejected'), true);
  assert.equal(await p.answer, 'rejected');
});

for (const kind of ['project-reflection', 'shikigami-reflection']) {
  for (const kinds of [[kind, 'user'], ['user', kind]]) {
    test(`${kinds.join(' then ')} preserves reflection source without self-approval`, async t => {
      const f = fixture(t);
      f.owners.set(f.a, binding('A', 'reflection'));
      f.start(f.a, 2, kinds[0]);
      f.emit('agent/inbox/claimed', { agent: f.a, turn: 2,
        message: { role: 'user', source: { kind: kinds[1] } } });
      const p = await f.ask(f.a);
      assert.equal(p.event.source.kind, kind);
      let settled = false; p.answer.then(() => { settled = true; });
      await Promise.resolve(); assert.equal(settled, false);
      assert.equal(f.controls.answer(binding('A', 'reflection'), p.event.approval_id, 'rejected'), true);
      assert.equal(await p.answer, 'rejected');
    });
  }
}

test('owner revisions must be nonnegative safe integers, never strings or booleans', async t => {
  for (const context_revision of ['7', '', true, false, null, undefined, -1, 1.5, NaN, Infinity,
    Number.MAX_SAFE_INTEGER + 1]) {
    const f = fixture(t), invalid = Object.freeze({ ...binding(), context_revision });
    f.owners.set(f.a, invalid);
    assert.equal(await f.controls.cancel(invalid), false, `revision ${String(context_revision)}`);
    assert.deepEqual(f.a.cancels, []);
    const gate = await f.waterfall('tools/pre-execute', { name: 'read', agent: f.a,
      signal: f.a.abort.signal, callId: 'c', arguments: { file_path: 'x' } });
    assert.equal(gate.kind, 'deny');
  }
  for (const context_revision of [0, Number.MAX_SAFE_INTEGER]) {
    const f = fixture(t), valid = Object.freeze({ ...binding(), context_revision });
    f.owners.set(f.a, valid);
    assert.equal(await f.controls.cancel(valid), true);
  }
});

test('string revision answers reject without consuming an integer-bound approval', async t => {
  const f = fixture(t), p = await f.ask(f.a);
  assert.equal(f.controls.answer({ ...binding(), context_revision: '7' }, p.event.approval_id, 'allowed-once'), false);
  assert.equal(f.controls.answer(binding(), p.event.approval_id, 'rejected'), true);
  assert.equal(await p.answer, 'rejected');
});

test('cancelling notification failure does not prevent native cancellation', async t => {
  const f = fixture(t, { publish(event) {
    if (event.status === 'cancelling') throw new Error('UI offline');
  } });
  assert.equal(await f.controls.cancel(binding()), true);
  assert.deepEqual(f.a.cancels, [{ kind: 'user' }]);
  assert.deepEqual(f.events.map(e => [e.type, e.status]), [['status', 'cancelling']]);
});

test('reentrant ownership change during cancelling publish cannot cancel the replacement run', async t => {
  let f;
  f = fixture(t, { publish(event) {
    if (event.status === 'cancelling') f.owners.set(f.a, binding('A', 'replacement'));
  } });
  assert.equal(await f.controls.cancel(binding()), false);
  assert.deepEqual(f.a.cancels, []);
});

test('only a native aborted turn emits final cancelled, independently of idle acknowledgement', async t => {
  const f = fixture(t), idle = deferred(); f.a.whenIdle = () => idle.promise;
  let acknowledged = false;
  const cancel = f.controls.cancel(binding()).then(result => { acknowledged = true; return result; });
  await Promise.resolve();
  assert.equal(acknowledged, false);
  assert.equal(f.events.some(e => e.type === 'cancelled'), false);
  f.session(f.a, 'turn/end', { turn: 1, reason: { kind: 'aborted', reason: { kind: 'user' } } }, 10);
  assert.equal(f.events.filter(e => e.type === 'cancelled').length, 1);
  assert.equal(acknowledged, false);
  idle.resolve(); assert.equal(await cancel, true);
  assert.equal(f.events.filter(e => e.type === 'cancelled').length, 1);
});
