import test from 'node:test';
import assert from 'node:assert/strict';

const module = await import('../history.mjs').catch(error => {
  if (error.code === 'ERR_MODULE_NOT_FOUND') return {};
  throw error;
});
const now = 1_793_340_000_123;
const message = (role, content) => ({ role, content });
const system = 'persona {{literal}}\n1. user: 2026-10-20 09:00\n2. assistant: time unknown';
function envelope(messages = [message('system', system), message('user', 'current')]) {
  return { schema_version: 1, profile_id: '角色甲', request_id: 'r1',
    context_revision: 0, origin: 'user', messages };
}
function history() {
  return envelope([message('system', system), message('user', 'old question'),
    message('assistant', 'old answer'), message('user', 'second question'),
    message('assistant', 'second answer'), message('user', 'current {{literal}}')]);
}
function build(input, options = { now }) {
  assert.equal(typeof module.buildHistorySeed, 'function', 'buildHistorySeed must be implemented');
  return module.buildHistorySeed(input, options);
}
function surface(seed) {
  return seed.filter(event => event.surfaceOp === 'append')
    .map(event => event.type === 'user/message' ? event.data : event.data.message);
}

test('history preserves role/text order, lifecycle coordinates and dense native sequences', () => {
  const { seed, trigger } = build(history());
  const pairTypes = ['turn/start', 'step/start', 'user/message', 'assistant/message',
    'step/end', 'turn/end'];
  assert.deepEqual(seed.map(event => event.type), [
    ...pairTypes.slice(0, 2), 'system/message', ...pairTypes.slice(2), ...pairTypes]);
  assert.deepEqual(seed.map(event => event.seq), Array.from({ length: 13 }, (_, i) => i));
  assert.equal(seed[2].data.turn, 1);
  assert.equal(seed[2].data.step, 1);
  assert.equal(seed.filter(event => event.type === 'system/message').length, 1);
  for (const event of seed) {
    for (const key of ['turn', 'step']) {
      if (Object.hasOwn(event.data, key)) assert.ok(event.data[key] > 0);
    }
  }
  assert.deepEqual(surface(seed).map(item => item.role), ['system', 'user', 'assistant', 'user', 'assistant']);
  assert.deepEqual(surface(seed).slice(1).map(item => item.content), [
    [{ type: 'text', text: 'old question' }], [{ type: 'text', text: 'old answer' }],
    [{ type: 'text', text: 'second question' }], [{ type: 'text', text: 'second answer' }],
  ]);
  for (const [start, turn, extra] of [[0, 1, 1], [7, 2, 0]]) {
    assert.deepEqual(seed[start].data, { turn });
    assert.deepEqual(seed[start + 1].data, { turn, step: 1 });
    assert.equal(seed[start + 3 + extra].data.turn, turn);
    assert.equal(seed[start + 3 + extra].data.step, 1);
    assert.deepEqual(seed[start + 4 + extra].data, { turn, step: 1 });
    assert.deepEqual(seed[start + 5 + extra].data, { turn, reason: { kind: 'completed' } });
  }
  assert.equal(trigger, 'current {{literal}}');
  assert.equal(JSON.stringify(seed).includes('current {{literal}}'), false);
  assert.ok(seed.filter(event => event.type.endsWith('/message')).every(event => event.surfaceOp === 'append'));
  assert.ok(seed.filter(event => !event.type.endsWith('/message')).every(event => !Object.hasOwn(event, 'surfaceOp')));
});

test('unknown historical attribution is an explicit local mapping, never current-provider metadata', () => {
  const input = { ...history(), provider: 'current-provider', model: 'current-model' };
  const { seed } = build(input);
  for (const event of seed.filter(item => item.type === 'assistant/message')) {
    assert.deepEqual(event.data.stream, []);
    assert.deepEqual(event.data.message.source, { kind: 'model',
      provider: 'shikigami-history/unknown-provider', model: 'unknown-model' });
    assert.deepEqual(Object.keys(event.data).sort(), ['message', 'step', 'stream', 'turn']);
    assert.deepEqual(Object.keys(event.data.message).sort(), ['content', 'id', 'role', 'source']);
  }
  for (const item of surface(seed).filter(item => item.role === 'user')) {
    assert.deepEqual(item.source, { kind: 'shikigami-history', form: 'recall' });
  }
  assert.deepEqual(surface(seed)[0].source, { kind: 'system-prompt' });
  assert.doesNotMatch(JSON.stringify(seed), /replayState|usage|signature|current-provider|current-model/);
});

test('system text is exact including literal braces/time index; native timestamps record import time only', () => {
  const { seed } = build(history());
  assert.deepEqual(surface(seed)[0].content, [{ type: 'text', text: system }]);
  assert.deepEqual(surface(build(history(), { now: now + 1 }).seed)[0].content,
    [{ type: 'text', text: system }]);
  assert.ok(seed.every(event => Number.isSafeInteger(event.time) && event.time === now));
});

test('fresh system/user returns an empty seed and untrimmed final trigger for each origin', () => {
  for (const origin of ['user', 'reflection', 'proactive']) {
    for (const text of ['', system]) {
      const input = { ...envelope([message('system', text), message('user', '  current\n')]), origin };
      assert.deepEqual(build(input), { seed: [], trigger: '  current\n' });
    }
  }
});

test('native message IDs are UUIDs unique within and between imports', () => {
  const ids = [...surface(build(history()).seed), ...surface(build(history()).seed)].map(item => item.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const id of ids) assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
});

test('input stays unchanged, outputs are detached and JSON round trips losslessly', () => {
  const input = history(), before = structuredClone(input);
  input.messages.forEach(Object.freeze);
  Object.freeze(input.messages);
  Object.freeze(input);
  const output = build(input);
  assert.deepEqual(input, before);
  assert.deepEqual(JSON.parse(JSON.stringify(output)), output);
  surface(output.seed)[0].content[0].text = 'changed';
  output.seed[3].data.content[0].text = 'changed';
  assert.deepEqual(input, before);
});

test('default event time is sampled from the actual import interval', () => {
  const start = Date.now(), { seed } = build(history(), {}), end = Date.now();
  assert.ok(seed.length > 0);
  assert.ok(seed.every(event => event.time >= start && event.time <= end));
});

test('valid ID/revision boundaries follow the Python envelope', () => {
  for (const id of [' 中 ', '😀'.repeat(256)]) {
    assert.doesNotThrow(() => build({ ...envelope(), profile_id: id, request_id: id,
      context_revision: Number.MAX_SAFE_INTEGER }));
  }
  assert.doesNotThrow(() => build(envelope(), { now: 0 }));
});

test('rejects invalid envelope metadata without coercion', () => {
  for (const value of [null, [], 'context', 1, {}]) assert.throws(() => build(value), TypeError);
  for (const schema_version of [undefined, 0, 2, '1', true]) {
    assert.throws(() => build({ ...envelope(), schema_version }), /schema/);
  }
  for (const key of ['profile_id', 'request_id']) {
    for (const value of [undefined, null, true, 1, '', ' \n', '😀'.repeat(257)]) {
      assert.throws(() => build({ ...envelope(), [key]: value }), new RegExp(key));
    }
  }
  for (const context_revision of [undefined, -1, 0.5, true, '0', NaN, Infinity, 2 ** 53]) {
    assert.throws(() => build({ ...envelope(), context_revision }), /revision/);
  }
  for (const origin of [undefined, 'assistant', '', null]) {
    assert.throws(() => build({ ...envelope(), origin }), /origin/);
  }
});

test('rejects nontext, unnormalized roles, extra message fields and absent/blank final user', () => {
  const s = message('system', system), u = message('user', 'q'), a = message('assistant', 'a');
  const invalid = [undefined, null, {}, 'messages', [], [s], [u], [s, a], [u, s],
    [s, u, s], [s, u, u], [s, a, u], [s, u, a, a, u],
    [s, message('tool', 'x'), u], [s, message('user', [])], [s, null],
    [s, { ...u, source: {} }], [s, { role: 'user' }], [s, message('user', ' \n')],
    [s, , u]];
  for (const messages of invalid) assert.throws(() => build({ ...envelope(), messages }), /messages|trigger/);
});

test('rejects unsafe timestamps and bounded message count/text size', () => {
  for (const value of [-1, -0, 1.5, NaN, Infinity, 2 ** 53, '123', null]) {
    assert.throws(() => build(envelope(), { now: value }), /now/);
  }
  assert.throws(() => build(envelope([message('system', 'x'.repeat(4 * 1024 * 1024)),
    message('user', 'q')])), /bound|limit/);
  const messages = [message('system', '')];
  for (let i = 0; i < 2048; i++) messages.push(message('user', 'q'), message('assistant', 'a'));
  messages.push(message('user', 'current'));
  assert.throws(() => build(envelope(messages)), /bound|limit/);
});
