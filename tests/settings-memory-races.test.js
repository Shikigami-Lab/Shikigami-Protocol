'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/settings.js'), 'utf8');
const response = (data, ok = true) => ({ ok, status: ok ? 200 : 500, json: async () => data, text: async () => 'failed' });
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness(fetch) {
  const toasts = [];
  const timers = new Map();
  let timerId = 0;
  const sandbox = {
    console, fetch, getBaseUrl: () => '',
    API_PATHS: new Proxy({}, { get: (_, key) => (...args) => [key, ...args].join('/') }),
    setTimeout: (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; },
    clearTimeout: id => timers.delete(id),
  };
  const mixin = vm.runInNewContext(source + '\nSettingsMixin;', sandbox);
  const model = Object.assign(mixin.data(), {
    t: key => key, $nextTick: fn => Promise.resolve().then(fn),
  });
  for (const [key, method] of Object.entries(mixin.methods)) model[key] = method.bind(model);
  model.showToast = (message, type) => toasts.push({ message, type });
  return { model, watch: mixin.watch, toasts, runDebounce() {
    for (const [id, { fn, ms }] of timers) {
      if (ms === 800) { timers.delete(id); fn(); }
    }
  } };
}

test('older Memory reads cannot replace facts, vectors, summaries, history or maintenance for the current profile', async () => {
  const delayed = [];
  const { model } = harness(url => {
    const [route, id] = url.split('/');
    const result = {
      memoryStatus: { id }, memoryFacts: { facts: [id] }, memoryVectors: { vectors: [id] },
      memorySummariesDays: { summaries: [id] }, sessionChatHistory: { messages: [id], total: 1 },
      memoryChangelog: { changelog: [id] }, memoryForgettingPreview: { id },
      memorySummariesPreview: { per_date: [id] },
    }[route] || {};
    if (id === 'a') return new Promise(resolve => delayed.push(() => resolve(response(result))));
    return Promise.resolve(response(result));
  });
  model.memoryProfileId = 'a';
  const old = [model.loadMemoryData('a'), model.loadForgettingPreview('a'), model.loadDiaryPreview('a')];
  model.memoryProfileId = 'b';
  await Promise.all([model.loadMemoryData('b'), model.loadForgettingPreview('b'), model.loadDiaryPreview('b')]);
  delayed.forEach(resolve => resolve());
  await Promise.all(old);
  for (const key of ['memoryFacts', 'memoryVectors', 'memorySummaries', 'chatHistory', 'memoryChangelog', 'diaryPreview']) {
    assert.deepEqual([...model[key]], ['b'], key);
  }
  assert.equal(model.memoryStatus.id, 'b');
  assert.equal(model.forgettingPreview.id, 'b');
  assert.equal(model.memoryContentProfileId, 'b');
});

test('Memory selection leaves Persona selection alone and loads its own settings form', async () => {
  const { model } = harness(async url => response({ memory_config: { extraction_weight_threshold: 0 } }));
  model.selectedProfileId = 'persona';
  model.profiles = [{ profile_id: 'persona' }, { profile_id: 'memory' }];
  model.selectProfile = id => { model.selectedProfileId = id; };
  await model.onMemorySettingsProfileChange('memory');
  assert.equal(model.selectedProfileId, 'persona');
  assert.equal(model.memoryProfileId, 'memory');
  assert.equal(model.memoryTabFormProfileId, 'memory');
  assert.equal(model.memoryTabProfileForm.extraction_weight_threshold, 0);
});

test('failed clear and context reset preserve messages and report an error', async () => {
  const { model, toasts } = harness(async () => response({}, false));
  model.currentSessionId = model.memoryProfileId = model.memoryContentProfileId = 'a';
  model.messages = [{ id: 1 }];
  model.chatHistory = [{ id: 1 }];
  await model._doClearHistory('a');
  await model._doClearShortMem('a');
  assert.equal(model.messages.length, 1);
  assert.equal(model.chatHistory.length, 1);
  assert.deepEqual(toasts.map(toast => toast.type), ['error', 'error']);
});

test('clear and reset reject an unsuccessful JSON result even on HTTP 200', async () => {
  const { model, toasts } = harness(async () => response({ ok: false, error: 'failed' }));
  model.currentSessionId = model.memoryProfileId = 'a';
  model.messages = [{ id: 1 }];
  model.memoryFacts = [{ id: 2 }];
  model.memoryVectors = [{ id: 3 }];
  model.memorySummaries = [{ date: '2026-01-01' }];
  await model._doClearHistory('a');
  await model._doClearShortMem('a');
  await model._doClearFacts('a');
  await model._doClearVectors('a');
  await model._doClearSummaries('a');
  assert.deepEqual([model.messages.length, model.memoryFacts.length, model.memoryVectors.length, model.memorySummaries.length], [1, 1, 1, 1]);
  assert.deepEqual(toasts.map(toast => toast.type), Array(5).fill('error'));
});

test('failed history paging retries the same offset and keeps the applied search', async () => {
  const calls = [];
  const { model } = harness(async url => {
    calls.push(url);
    return response({ messages: [calls.length], total: 10 }, calls.length !== 2);
  });
  model.memoryProfileId = 'a';
  model.chatHistoryLimit = 1;
  model.chatHistorySearch = 'applied';
  await model.loadChatHistory('a');
  model.chatHistorySearch = 'draft';
  await model.chatHistoryLoadMore();
  assert.equal(model.chatHistory.length, 1);
  await model.chatHistoryLoadMore();
  assert.deepEqual(calls, ['sessionChatHistory/a/1/0/applied', 'sessionChatHistory/a/1/1/applied', 'sessionChatHistory/a/1/1/applied']);
  assert.deepEqual([...model.chatHistory], [1, 3]);
});

test('a successful short-context reset refreshes its marker without duplicating loaded pages', async () => {
  const calls = [];
  const { model } = harness(async (url, options) => {
    calls.push(url);
    if (options?.method === 'DELETE') return response({ ok: true });
    return response({ messages: [{ id: 9 }], total: 4, context_since_ts: 123 });
  });
  model.memoryProfileId = 'a';
  model.chatHistory = [{ id: 9 }, { id: 8 }];
  model.chatHistoryTotal = 4;
  await model._doClearShortMem('a');
  assert.deepEqual(calls, ['sessionHistoryRecent/a', 'sessionChatHistory/a/1/0/']);
  assert.deepEqual(model.chatHistory, [{ id: 9 }, { id: 8 }]);
  assert.equal(model.chatHistoryContextSince, 123);
});

test('context reset wins over the old marker in an in-flight history page', async () => {
  let finishPage;
  const { model } = harness((url, options) => {
    if (options?.method === 'DELETE') return Promise.resolve(response({ ok: true }));
    if (url === 'sessionChatHistory/a/1/1/') return new Promise(resolve => { finishPage = resolve; });
    return Promise.resolve(response({ messages: [{ id: 9 }], total: 4, context_since_ts: 123 }));
  });
  model.memoryProfileId = 'a';
  model.chatHistoryLimit = 1;
  model.chatHistory = [{ id: 9 }];
  model.chatHistoryTotal = 4;
  const page = model.chatHistoryLoadMore();
  await model._doClearShortMem('a');
  finishPage(response({ messages: [{ id: 8 }], total: 4, context_since_ts: 0 }));
  await page;
  assert.deepEqual(model.chatHistory, [{ id: 9 }, { id: 8 }]);
  assert.equal(model.chatHistoryContextSince, 123);
});

test('Memory persona save sends inherit and zero, keeps extraction preset, and omits Persona prompts', async () => {
  const writes = [];
  const { model } = harness(async (_url, options) => {
    if (options) writes.push(JSON.parse(options.body));
    return response({ ok: true, memory_config: { enabled: false, max_history_turns: 40, extraction_llm_preset: 'synthetic' } });
  });
  await model.onMemorySettingsProfileChange('a');
  assert.equal(model.memoryTabProfileForm.extraction_llm_preset, 'synthetic');
  model.memoryTabProfileForm.enabled = null;
  model.memoryTabProfileForm.max_history_turns = '';
  model.memoryTabProfileForm.extraction_weight_threshold = 0;
  model.memoryTabProfileForm.recent_fact_quota = 0;
  await model.saveMemoryTabProfileConfig();
  assert.equal(writes[0].enabled, null);
  assert.equal(writes[0].max_history_turns, null);
  assert.equal(writes[0].extraction_weight_threshold, 0);
  assert.equal(writes[0].recent_fact_quota, 0);
  assert.equal(writes[0].extraction_llm_preset, 'synthetic');
  assert.equal(Object.hasOwn(writes[0], 'extraction_prompt'), false);
});

test('global Memory persists zero-valued fields and supplies a finite fallback for an empty numeric input', async () => {
  let body;
  const { model } = harness(async (_url, options) => {
    body = JSON.parse(options.body);
    return response({ ok: true });
  });
  Object.assign(model.memoryGlobalForm, {
    extraction_weight_threshold: 0, recent_fact_quota: '', semantic_fact_quota: 0, semantic_distance: 0,
    daily_run_at_minute: 0, daily_decay_min_weight: 0, daily_consolidation_weight_below: 0,
  });
  await model.saveMemoryGlobalConfig();
  for (const key of ['extraction_weight_threshold', 'semantic_fact_quota', 'semantic_distance',
    'daily_run_at_minute', 'daily_decay_min_weight', 'daily_consolidation_weight_below']) {
    assert.equal(body[key], 0, key);
  }
  assert.equal(body.recent_fact_quota, 3);
});

test('late Persona engine GET cannot overwrite the selected profile or its defaults', async () => {
  let finishA;
  const { model } = harness(url => url.endsWith('/a')
    ? new Promise(resolve => { finishA = resolve; })
    : Promise.resolve(response({ emotion_freq: 22, global_defaults: { emotion_freq: 33 } })));
  model.selectedProfileId = model.profileForm.profile_id = 'a';
  const old = model.loadProfileEngineConfig('a');
  model.selectedProfileId = model.profileForm.profile_id = 'b';
  await model.loadProfileEngineConfig('b');
  finishA(response({ emotion_freq: 1, global_defaults: { emotion_freq: 2 } }));
  await old;
  assert.equal(model.profileEnginesForm.emotion_freq, 22);
  assert.equal(model.profileEngineGlobalDefaults.emotion_freq, 33);
});

test('debounced Persona engine save keeps the edited profile and payload across a profile switch', async () => {
  const writes = [];
  const { model, watch, runDebounce } = harness(async (url, options) => {
    if (options) writes.push([url, JSON.parse(options.body)]);
    return response({ ok: true, emotion_freq: 5 });
  });
  model.selectedProfileId = model.profileForm.profile_id = 'a';
  await model.loadProfileEngineConfig('a');
  model.profileEnginesForm.emotion_freq = 13;
  watch.profileEnginesForm.handler.call(model);
  model.selectedProfileId = model.profileForm.profile_id = 'b';
  await model.loadProfileEngineConfig('b');
  runDebounce();
  await tick();
  assert.equal(writes.length, 1);
  assert.equal(writes[0][0], 'profileEngineConfig/a');
  assert.equal(writes[0][1].emotion_freq, 13);
});

test('Persona editor keeps a queued profile draft when switching away and back', async () => {
  const writes = [];
  const { model, watch, runDebounce } = harness(async (url, options) => {
    if (options) writes.push([url, JSON.parse(options.body)]);
    return response({ ok: true });
  });
  model.profiles = [{ profile_id: 'a', display_name: 'Original' }, { profile_id: 'b' }];
  model.loadProfiles = model.loadSessions = async () => {};
  model.selectProfile('a');
  model.profileForm.display_name = 'Latest A';
  watch.profileForm.handler.call(model);
  model.selectProfile('b');
  model.selectProfile('a');
  assert.equal(model.profileForm.display_name, 'Latest A');
  model.profileForm.base_prompt = 'New prompt';
  watch.profileForm.handler.call(model);
  runDebounce();
  await tick();
  assert.equal(writes.length, 1);
  assert.equal(writes[0][0], 'profile/a');
  assert.equal(writes[0][1].display_name, 'Latest A');
  assert.equal(writes[0][1].base_prompt, 'New prompt');
});

test('late Reflection reload cannot overwrite an edit queued during the request', async () => {
  let finishGet;
  const writes = [];
  const { model, watch, runDebounce } = harness((_url, options) => {
    if (!options) return new Promise(resolve => { finishGet = resolve; });
    writes.push(JSON.parse(options.body).reflection_interval);
    return Promise.resolve(response({ ok: true }));
  });
  model._reflectionFormLoaded = true;
  const reload = model.loadReflectionConfig();
  model.reflectionForm.reflection_interval = 280;
  watch.reflectionForm.handler.call(model);
  finishGet(response({ reflection_interval: 100 }));
  await reload;
  runDebounce();
  await tick();
  assert.equal(model.reflectionForm.reflection_interval, 280);
  assert.deepEqual(writes, [280]);
});

test('a late Persona A segment load cannot appear in Persona B or be saved to B', async () => {
  const writes = [];
  let finishA;
  const segments = id => ({ segments: [{ segment_id: id, inject_into: 'chat', content: id }], custom_segments: [] });
  const { model } = harness((url, options) => {
    if (options) {
      writes.push([url, JSON.parse(options.body)]);
      return Promise.resolve(response({ ok: true }));
    }
    if (url.endsWith('/a')) return new Promise(resolve => { finishA = resolve; });
    return Promise.resolve(response(segments('b')));
  });
  model.selectedProfileId = model.profileForm.profile_id = 'a';
  const old = model.loadSegmentConfig('a');
  model.selectedProfileId = model.profileForm.profile_id = 'b';
  await model.loadSegmentConfig('b');
  finishA(response(segments('a')));
  await old;
  await model.saveSegmentConfig();
  assert.equal(model.segmentList[0].segment_id, 'b');
  assert.equal(writes[0][0], 'profileSegments/b');
  assert.equal(writes[0][1].segments[0].content, 'b');
});

test('queued segment saves keep the edited profile and the latest immutable payload', async () => {
  let finishFirst;
  const writes = [];
  const { model, watch, runDebounce } = harness((url, options) => {
    if (!options) return Promise.resolve(response({ custom_segments: [{ segment_id: 'custom', inject_into: 'chat', content: url }] }));
    writes.push([url, JSON.parse(options.body).custom_segments[0].content]);
    if (writes.length === 1) return new Promise(resolve => { finishFirst = resolve; });
    return Promise.resolve(response({ ok: true }));
  });
  model._profileFormLoaded = true;
  model.selectedProfileId = model.profileForm.profile_id = 'a';
  await model.loadSegmentConfig('a');
  model.customSegmentList[0].content = 'first A edit';
  const first = model.saveSegmentConfig();
  await tick();
  model.customSegmentList[0].content = 'latest A edit';
  watch.customSegmentList.handler.call(model);
  model.selectedProfileId = model.profileForm.profile_id = 'b';
  await model.loadSegmentConfig('b');
  runDebounce();
  finishFirst(response({ ok: true }));
  await first;
  await tick();
  assert.deepEqual(writes, [['profileSegments/a', 'first A edit'], ['profileSegments/a', 'latest A edit']]);
});

test('late Reflection profile reads and debounced writes stay with their own profiles', async () => {
  let finishA;
  const writes = [];
  const { model, watch, runDebounce } = harness((url, options) => {
    if (options) {
      writes.push([url, JSON.parse(options.body).custom_prompt]);
      return Promise.resolve(response({ ok: true }));
    }
    if (url.endsWith('/a')) return new Promise(resolve => { finishA = resolve; });
    return Promise.resolve(response({ custom_prompt: url }));
  });
  model.selectedProfileId = 'a';
  const old = model.loadReflectionProfileConfig('a');
  model.selectedProfileId = 'b';
  await model.loadReflectionProfileConfig('b');
  model.reflectionProfileForm.custom_prompt = 'edited B';
  watch.reflectionProfileForm.handler.call(model);
  model.selectedProfileId = 'c';
  await model.loadReflectionProfileConfig('c');
  finishA(response({ custom_prompt: 'stale A' }));
  await old;
  runDebounce();
  await tick();
  assert.equal(model.reflectionProfileForm.custom_prompt, 'profileReflectionConfig/c');
  assert.deepEqual(writes, [['profileReflectionConfig/b', 'edited B']]);
});

for (const [method, form, key, first, last] of [
  ['saveSystemConfig', 'systemForm', 'log_level', 'info', 'debug'],
  ['saveSystemEnv', 'systemEnvForm', 'http_proxy', 'first', 'last'],
  ['saveReflectionConfig', 'reflectionForm', 'reflection_interval', 120, 360],
]) {
  test(method + ' serializes the latest edit behind an in-flight save', async () => {
    const writes = [];
    let finishFirst;
    const { model } = harness((_url, options) => {
      writes.push(JSON.parse(options.body)[key]);
      if (writes.length === 1) return new Promise(resolve => { finishFirst = resolve; });
      return Promise.resolve(response({ ok: true }));
    });
    model[form][key] = first;
    const old = model[method](true);
    await tick();
    model[form][key] = last;
    const latest = model[method](true);
    finishFirst(response({ ok: true }));
    await Promise.all([old, latest]);
    assert.deepEqual(writes, [first, last]);
  });
}
