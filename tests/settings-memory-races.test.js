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
  for (const [key, getter] of Object.entries(mixin.computed)) {
    if (typeof getter === 'function' && !(key in model)) Object.defineProperty(model, key, { get: getter.bind(model) });
  }
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
  await model._doClearSummaries('a');
  assert.deepEqual([model.messages.length, model.memoryFacts.length, model.memoryVectors.length, model.memorySummaries.length], [1, 1, 1, 1]);
  assert.deepEqual(toasts.map(toast => toast.type), Array(4).fill('error'));
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

test('fact filters keep active records by default and make archived, expired and review records visible', () => {
  const { model } = harness(async () => response({}));
  const { computed } = vm.runInNewContext(source + '\nSettingsMixin;');
  model.memoryFacts = [
    { id: 'active' },
    { id: 'review', review_note: 'Check original' },
    { id: 'expired', expires_at: 1 },
    { id: 'archived', archived: true, archive_reason: 'low_value' },
    { id: 'experience', kind: 'experience' },
    { id: 'experience-archived', kind: 'experience', archived: true, review_note: 'Check original' },
    { id: 'experience-expired', kind: 'experience', expires_at: 1 },
  ];
  const visible = () => Array.from(computed.sortedMemoryFacts.call(model), fact => fact.id);
  assert.deepEqual(visible(), ['active', 'review']);
  model.factFilterStatus = 'experiences';
  assert.deepEqual(visible(), ['experience']);
  model.factFilterStatus = 'archived';
  assert.deepEqual(visible(), ['expired', 'archived', 'experience-archived', 'experience-expired']);
  model.factFilterStatus = 'review';
  assert.deepEqual(visible(), ['review', 'experience-archived']);
});

test('fact evidence loads on demand, deduplicates expansion and keeps available and unavailable rows', async () => {
  const urls = [];
  let finishEvidence;
  const fact = { id: 'entry', kind: 'experience' };
  const { model } = harness(url => {
    urls.push(url);
    if (url === 'memoryFacts/a') return Promise.resolve(response({ facts: [fact] }));
    return new Promise(resolve => { finishEvidence = resolve; });
  });
  model.selectMemoryProfile('a');
  await model.loadMemoryFacts('a');
  assert.deepEqual(urls, ['memoryFacts/a']);
  const load = model.loadMemoryFactEvidence(fact);
  assert.equal(model.memoryFactEvidence.get(fact.id).status, 'loading');
  await model.loadMemoryFactEvidence(fact);
  const items = [
    { source: 'single', id: 1, content: 'Original <message>\nSecond line', available: true, start_char: 0, end_char: 29 },
    { source: 'single', id: 2, available: false },
  ];
  finishEvidence(response({ profile_id: 'a', fact_id: 'entry', items }));
  await load;
  await model.loadMemoryFactEvidence(fact);
  assert.equal(model.memoryFactEvidence.get(fact.id).status, 'ready');
  assert.deepEqual(model.memoryFactEvidence.get(fact.id).items, items);
  assert.deepEqual(urls, ['memoryFacts/a', 'memoryFactEvidence/a/entry']);
});

test('evidence for a previous persona selection cannot replace evidence for the same fact ID', async () => {
  const pending = [];
  const { model } = harness(() => new Promise(resolve => pending.push(resolve)));
  const select = id => {
    model.selectMemoryProfile(id);
    const fact = { id: 'entry' };
    model.memoryFacts = [fact];
    return fact;
  };
  const firstFact = select('a');
  const old = model.loadMemoryFactEvidence(firstFact);
  const secondFact = select('b');
  const other = model.loadMemoryFactEvidence(secondFact);
  const currentFact = select('a');
  await model.loadMemoryFactEvidence(firstFact);
  const current = model.loadMemoryFactEvidence(currentFact);
  const payload = (profile_id, content) => ({ profile_id, fact_id: 'entry', items: [{ available: true, content }] });
  pending[2](response(payload('a', 'Current evidence')));
  await current;
  pending[0](response(payload('a', 'Old evidence')));
  pending[1](response(payload('b', 'Other evidence')));
  await Promise.all([old, other]);
  assert.equal(pending.length, 3);
  assert.equal(model.memoryFactEvidence.get('entry').items[0].content, 'Current evidence');
});

test('fact evidence rejects mismatched responses and allows reopening after a failed request', async () => {
  const replies = [
    response({ profile_id: 'b', fact_id: 'entry', items: [{ available: true, content: 'Other evidence' }] }),
    response({}, false),
    response({ profile_id: 'a', fact_id: 'entry', items: [] }),
  ];
  const { model } = harness(async () => replies.shift());
  model.selectMemoryProfile('a');
  const fact = { id: 'entry' };
  model.memoryFacts = [fact];
  for (let attempt = 0; attempt < 2; attempt++) {
    await model.loadMemoryFactEvidence(fact);
    assert.equal(model.memoryFactEvidence.get(fact.id).status, 'error');
    assert.equal(model.memoryFactEvidence.get(fact.id).items, undefined);
  }
  await model.loadMemoryFactEvidence(fact);
  assert.equal(model.memoryFactEvidence.get(fact.id).status, 'ready');
  assert.deepEqual(model.memoryFactEvidence.get(fact.id).items, []);
});

test('refreshing facts invalidates evidence fetched for the previous list', async () => {
  const refreshed = { id: 'entry', content: 'Updated memory' };
  let finishEvidence;
  const { model } = harness(url => url === 'memoryFacts/a'
    ? Promise.resolve(response({ facts: [refreshed] }))
    : new Promise(resolve => { finishEvidence = resolve; }));
  model.selectMemoryProfile('a');
  const original = { id: 'entry', content: 'Earlier memory' };
  model.memoryFacts = [original];
  const old = model.loadMemoryFactEvidence(original);
  const revision = model.memoryFactsRevision;
  await model.loadMemoryFacts('a');
  finishEvidence(response({ profile_id: 'a', fact_id: 'entry', items: [{ available: true, content: 'Old evidence' }] }));
  await old;
  assert.equal(model.memoryFacts[0], refreshed);
  assert.ok(model.memoryFactsRevision > revision);
  assert.equal(model.memoryFactEvidence.has('entry'), false);
});

test('version comparison preserves both full texts and highlights the changed span', () => {
  const { model } = harness(async () => response({}));
  const parts = model.memoryFactVersionDiff('😀 Draft\nkept', '😀 Revised\nkept');
  assert.deepEqual({ ...parts }, { prefix: '😀 ', before: 'Draft', after: 'Revised', suffix: '\nkept' });
  assert.deepEqual({ ...model.memoryFactVersionDiff('Same', 'Same') }, {
    prefix: 'Same', before: '', after: '', suffix: '',
  });
  assert.deepEqual({ ...model.memoryFactVersionDiff('End', 'New End') }, {
    prefix: '', before: '', after: 'New ', suffix: 'End',
  });
});

test('each earlier version compares against the next saved text, ending with the current text', () => {
  const { model } = harness(async () => response({}));
  const comparisons = model.memoryFactVersionComparisons({
    versions: [{ content: 'Draft', updated_at: 1 }, { content: 'Revision', updated_at: 2 }],
    content: 'Final',
  });
  assert.deepEqual(Array.from(comparisons, ({ before, after, updated_at }) => [before, after, updated_at]), [
    ['Draft', 'Revision', 1], ['Revision', 'Final', 2],
  ]);
});

test('malformed historical entries are skipped without renumbering restorable versions', () => {
  const { model } = harness(async () => response({}));
  const comparisons = model.memoryFactVersionComparisons({
    versions: [null, { content: 'First' }, { content: {} }, false, { content: 'Second' }, {}],
    content: 'Latest',
  });
  assert.deepEqual(Array.from(comparisons, version => version.version_index), [1, 4]);
  const texts = version => [version.prefix + version.before + version.suffix, version.prefix + version.after + version.suffix];
  assert.deepEqual(Array.from(comparisons, texts), [['First', 'Second'], ['Second', 'Latest']]);
});

test('a conflicting save uses the original edit revision and preserves the visible draft', async () => {
  const calls = [];
  const { model } = harness(async (url, options) => {
    calls.push([url, JSON.parse(options.body)]);
    return { ...response({}, false), status: 409 };
  });
  model.selectMemoryProfile('a');
  const fact = { id: 'entry', content: 'Before', category: 'other', weight: 1, action_revision: 'original' };
  model.memoryFacts = [fact];
  model.startEditMemoryFact(fact);
  const draft = model.memoryEditingFact;
  draft.content = 'My edit';
  model.memoryFacts = [{ ...fact, content: 'Concurrent edit', action_revision: 'later' }];
  await model.saveMemoryFact();
  assert.deepEqual(calls, [['memoryFactById/a/entry', {
    content: 'My edit', category: 'other', weight: 1, expected_revision: 'original',
  }]]);
  assert.equal(model.memoryEditingFact, draft);
  assert.equal(draft.content, 'My edit');
  assert.equal(draft.conflict, true);
  await model.saveMemoryFact();
  assert.equal(calls.length, 1);
});

test('pin updates carry the visible revision and refresh after a conflict', async () => {
  const calls = [];
  const fact = { id: 'entry', pinned: false, action_revision: 'old' };
  const refreshed = { ...fact, action_revision: 'new' };
  const { model, toasts } = harness(async (url, options) => {
    calls.push([url, options?.body && JSON.parse(options.body)]);
    if (options?.method === 'PUT') return { ...response({}, false), status: 409 };
    return response({ facts: [refreshed] });
  });
  model.selectMemoryProfile('a');
  model.memoryFacts = [fact];
  await model.toggleMemoryFactPin(fact);
  assert.deepEqual(calls, [
    ['memoryFactById/a/entry', { pinned: true, expected_revision: 'old' }], ['memoryFacts/a', undefined],
  ]);
  assert.equal(model.memoryFacts[0], refreshed);
  assert.deepEqual(toasts, [{ message: 'toastMemoryFactPinConflict', type: 'error' }]);
});

test('archive recall labels distinguish cold memory, live merge targets and unavailable records', () => {
  const { model } = harness(async () => response({}));
  model.memoryFacts = [{ id: 'live', content: 'Retained original' }, { id: 'expired', expires_at: 1 }];
  const label = (archive_reason, merged_into) => model.memoryArchiveRecallLabel({ archive_reason, merged_into });
  assert.equal(label('cold_memory'), 'memArchiveRecallCold');
  assert.equal(label('merged', 'live'), 'memArchiveRecallMerged');
  assert.equal(label('merged', 'expired'), 'memArchiveRecallUnavailable');
  assert.equal(label('merged', 'deleted'), 'memArchiveRecallUnavailable');
  assert.equal(model.memoryArchiveRecallLabel({
    archive_reason: 'merged', merged_into: 'live', expires_at: 1,
  }), 'memArchiveRecallUnavailable');
  for (const reason of ['low_value', 'unsupported', undefined]) {
    assert.equal(label(reason), 'memArchiveRecallUnavailable');
  }
});

test('curation posts a bodyless preview, counts its actions and only writes after explicit apply', async () => {
  const calls = [];
  const items = [
    { fact_id: 'one', action: 'keep', content: 'Keep original' },
    { fact_id: 'two', action: 'merge', content: 'Merge original', reason: 'Same topic', target_id: 'one', target_content: 'Keep original' },
    { fact_id: 'three', action: 'archive', content: 'Archive original', archive_reason: 'low_value' },
    { fact_id: 'four', action: 'review', content: 'Review original', reason: 'Check evidence' },
  ];
  const { model } = harness(async (url, options) => {
    calls.push([url, options]);
    if (url === 'memoryCurationPreview/a') return response({ profile_id: 'a', preview_id: 'p1', items });
    if (url === 'memoryCurationApply/a') return response({ ok: true, merged: 1, archived: 0, reviewed: 1 });
    if (url === 'memoryFacts/a') return response({ facts: [{ id: 'two', archived: true }] });
    return response({});
  });
  const { computed } = vm.runInNewContext(source + '\nSettingsMixin;');
  model.selectMemoryProfile('a');
  await model.loadMemoryCurationPreview('a');
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].method, 'POST');
  assert.equal(calls[0][1].body, undefined);
  assert.deepEqual(model.memoryCurationPreview.items, items);
  assert.deepEqual({ ...computed.memoryCurationCounts.call(model) }, { keep: 1, merge: 1, archive: 1, review: 1 });
  assert.deepEqual([...model.memoryCurationSelection], ['two', 'three', 'four']);
  model.memoryCurationSelection = ['two', 'four'];
  await model.applyMemoryCuration();
  assert.equal(calls[1][0], 'memoryCurationApply/a');
  assert.equal(calls[1][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[1][1].body), { preview_id: 'p1', fact_ids: ['two', 'four'] });
  assert.equal(model.memoryCurationPreview, null);
  assert.equal(model.memoryCurationResult.merged, 1);
  assert.equal(model.memoryFacts[0].archived, true);
});

test('the old-store preview sends one explicit cursor request per page and never creates an applicable preview', async () => {
  const calls = [];
  const first = {
    profile_id: 'a', items: [{ fact_id: 'one', action: 'archive' }],
    coverage: { total: 2, offset: 0, covered: 1 }, next_cursor: 'opaque:next',
  };
  const last = {
    profile_id: 'a', items: [{ fact_id: 'two', action: 'keep' }],
    coverage: { total: 2, offset: 1, covered: 2 }, next_cursor: null,
  };
  let finishFirst;
  const { model } = harness((url, options) => {
    calls.push([url, options.method, JSON.parse(options.body)]);
    return calls.length === 1 ? new Promise(resolve => { finishFirst = resolve; }) : Promise.resolve(response(last));
  });
  model.selectMemoryProfile('a');
  assert.deepEqual(calls, []);
  const pending = model.loadMemoryBulkCurationPage('a');
  await model.loadMemoryBulkCurationPage('a');
  assert.deepEqual(calls, [['memoryCurationBulkPreview/a', 'POST', { cursor: null }]]);
  finishFirst(response(first));
  await pending;
  assert.equal(model.memoryBulkCurationPage, first);
  assert.equal(calls.length, 1);
  await model.applyMemoryCuration();
  assert.equal(calls.length, 1);
  await model.loadMemoryBulkCurationPage('a');
  await model.loadMemoryBulkCurationPage('a');
  assert.deepEqual(calls[1], ['memoryCurationBulkPreview/a', 'POST', { cursor: 'opaque:next' }]);
  assert.equal(calls.length, 2);
  assert.equal(model.memoryBulkCurationPage, last);
  assert.equal(model.memoryCurationPreview, null);
  assert.deepEqual([...model.memoryCurationSelection], []);
});

test('review keep sends the visible revision, and archive requires an explicit permitted reason', async () => {
  const writes = [];
  const plain = { id: 'entry', review_note: 'Verify the original', action_revision: 'r1' };
  const { model } = harness(async (url, options) => {
    if (options?.method === 'POST') {
      writes.push([url, JSON.parse(options.body)]);
      return response({ ok: true });
    }
    return response({});
  });
  model.selectMemoryProfile('a');
  model.memoryFacts = [plain];
  await model.reviewMemoryFact(plain, 'archive');
  model.startMemoryFactArchive(plain);
  assert.deepEqual(writes, []);
  await model.reviewMemoryFact(plain, 'archive');
  model.memoryFactActionDraft.reason = 'cold_memory';
  await model.reviewMemoryFact(plain, 'archive');
  assert.deepEqual(writes, []);
  model.memoryFactActionDraft.reason = 'unsupported';
  await model.reviewMemoryFact(plain, 'archive');
  assert.deepEqual(writes, [['memoryFactReview/a/entry', {
    decision: 'archive', archive_reason: 'unsupported', expected_revision: 'r1',
  }]]);
  const next = { ...plain, action_revision: 'r2' };
  model.memoryFacts = [next];
  await model.reviewMemoryFact(plain, 'keep');
  await model.reviewMemoryFact(next, 'keep');
  assert.deepEqual(writes[1], ['memoryFactReview/a/entry', { decision: 'keep', expected_revision: 'r2' }]);
  assert.equal(writes.length, 2);
});

test('cold archive requires a completed, evidence-backed experience and protected rows cannot be archived', () => {
  const { model } = harness(async () => response({}));
  const experience = { kind: 'experience', content: 'We discussed the issue.', occurred_from: '2026-01-02',
    occurred_to: '2026-01-03', evidence: [
      { source: 'single', id: 1, timestamp: 1767312000, speaker_role: 'user', assertion: 'self_statement' },
    ] };
  const reasons = fact => Array.from(model.memoryFactArchiveReasons(fact));
  assert.deepEqual(reasons(experience), ['low_value', 'unsupported', 'cold_memory']);
  for (const overrides of [
    { evidence: [{ ...experience.evidence[0], id: 0 }] }, { occurred_to: '2026-01-01' },
    { occurred_to: '2026-02-31' }, { event_date: '2026-01-02' }, { expires_at: 9999999999 },
    { content: 'We spoke yesterday.' },
  ]) assert.deepEqual(reasons({ ...experience, ...overrides }), ['low_value', 'unsupported']);
  for (const overrides of [{ pinned: true }, { source: 'manual' }, { is_manual: true }]) {
    assert.deepEqual(reasons({ ...experience, ...overrides }), []);
  }
});

test('restore requires confirmation of a specific version and refreshes the displaced current version into history', async () => {
  const writes = [];
  const before = { id: 'entry', content: 'Current', kind: 'experience', action_revision: 'r1',
    versions: [{ content: 'First' }, { content: 'Second' }] };
  const after = { ...before, content: 'Second', action_revision: 'r2', versions: [...before.versions, { content: 'Current' }] };
  const diary = { date: '2026-11-05', experience_ids: ['entry'] };
  const { model } = harness(async (url, options) => {
    if (url === 'memoryFacts/a') return response({ facts: [after] });
    if (url.startsWith('memorySummariesDays/a/')) return response({ summaries: [diary] });
    if (options?.method === 'POST') writes.push([url, JSON.parse(options.body)]);
    return response({ ok: true });
  });
  model.selectMemoryProfile('a');
  model.memoryFacts = [before];
  model.factFilterStatus = 'experiences';
  await model.restoreMemoryFactVersion(before, 1);
  model.startMemoryFactVersionRestore(before, 1);
  await model.restoreMemoryFactVersion(before, 0);
  assert.deepEqual(writes, []);
  await model.restoreMemoryFactVersion(before, 1);
  assert.deepEqual(writes, [['memoryFactVersionRestore/a/entry', { version_index: 1, expected_revision: 'r1' }]]);
  assert.equal(model.memoryFacts[0].content, 'Second');
  assert.equal(model.memoryFacts[0].versions.at(-1).content, 'Current');
  assert.equal(model.memorySummaries[0], diary);
  assert.equal(model.factFilterStatus, 'experiences');
  assert.equal(model.memoryFactActionDraft, null);
});

test('switching personas clears fact confirmations and ignores a late review response', async () => {
  let finishReview;
  const requests = [];
  const { model, toasts } = harness(url => {
    requests.push(url);
    return new Promise(resolve => { finishReview = resolve; });
  });
  const before = { id: 'entry', review_note: 'Check', action_revision: 'a' };
  const current = { ...before, action_revision: 'b' };
  model.selectMemoryProfile('a');
  model.memoryFacts = [before];
  model.startMemoryFactArchive(before);
  model.memoryFactActionDraft.reason = 'low_value';
  const pending = model.reviewMemoryFact(before, 'archive');
  model.selectMemoryProfile('b');
  model.memoryFacts = [current];
  model.startMemoryFactArchive(current);
  finishReview(response({ ok: true }));
  await pending;
  await model.reviewMemoryFact(before, 'keep');
  assert.equal(model.memoryFactActionDraft.fact, current);
  assert.equal(model.memoryFactActionDraft.reason, '');
  assert.deepEqual(requests, ['memoryFactReview/a/entry']);
  assert.deepEqual(toasts, []);
});

test('bulk promotion only sends the current actionable row and still requires standard Apply', async () => {
  const writes = [];
  const item = { fact_id: 'entry', action: 'archive' };
  const keep = { fact_id: 'keep', action: 'keep' };
  const { model } = harness(async (url, options) => {
    if (options?.method === 'POST') writes.push([url, JSON.parse(options.body)]);
    if (url === 'memoryCurationBulkPromote/a') return response({ profile_id: 'a', preview_id: 'standard', items: [item, keep] });
    return response({ ok: true });
  });
  model.selectMemoryProfile('a');
  const page = { profile_id: 'a', page_token: 'page', items: [item, keep], next_cursor: null };
  model.memoryBulkCurationPage = page;
  await model.promoteMemoryBulkCurationItem(page, keep);
  await model.promoteMemoryBulkCurationItem({ ...page }, item);
  assert.deepEqual(writes, []);
  await model.promoteMemoryBulkCurationItem(page, item);
  assert.deepEqual(writes, [['memoryCurationBulkPromote/a', { page_token: 'page', fact_id: 'entry' }]]);
  assert.deepEqual([...model.memoryCurationSelection], ['entry']);
  model.memoryCurationSelection = [];
  await model.applyMemoryCuration();
  assert.equal(writes.length, 1);
  model.memoryCurationSelection = ['entry'];
  await model.applyMemoryCuration();
  assert.deepEqual(writes[1], ['memoryCurationApply/a', { preview_id: 'standard', fact_ids: ['entry'] }]);
  assert.equal(model.memoryBulkCurationPage, null);
  assert.equal(model.memoryBulkCurationRestartRequired, true);
});

test('promotion conflict clears a completed bulk page and the next explicit request starts at the first page', async () => {
  const calls = [];
  const item = { fact_id: 'entry', action: 'archive' };
  const { model } = harness(async (url, options) => {
    calls.push([url, JSON.parse(options.body)]);
    if (url === 'memoryCurationBulkPromote/a') return { ...response({}, false), status: 409 };
    return response({ profile_id: 'a', items: [], coverage: { total: 0, offset: 0, covered: 0 }, next_cursor: null });
  });
  model.selectMemoryProfile('a');
  const page = { profile_id: 'a', page_token: 'page', items: [item], next_cursor: null };
  model.memoryBulkCurationPage = page;
  await model.promoteMemoryBulkCurationItem(page, item);
  assert.equal(model.memoryBulkCurationPage, null);
  assert.equal(model.memoryBulkCurationRestartRequired, true);
  assert.equal(calls.length, 1);
  await model.loadMemoryBulkCurationPage('a');
  assert.deepEqual(calls[1], ['memoryCurationBulkPreview/a', { cursor: null }]);
  assert.equal(model.memoryBulkCurationRestartRequired, false);
});

test('a late bulk promotion cannot install a normal preview after switching personas', async () => {
  let finish;
  const { model } = harness(() => new Promise(resolve => { finish = resolve; }));
  const item = { fact_id: 'entry', action: 'merge' };
  model.selectMemoryProfile('a');
  const page = { profile_id: 'a', page_token: 'page', items: [item] };
  model.memoryBulkCurationPage = page;
  const pending = model.promoteMemoryBulkCurationItem(page, item);
  model.selectMemoryProfile('b');
  finish(response({ profile_id: 'a', preview_id: 'late', items: [item] }));
  await pending;
  assert.equal(model.memoryCurationPreview, null);
  assert.equal(model.memoryBulkCurationPage, null);
  assert.equal(model.memoryCurationPending, '');
});

test('diary links jump to the matching filter, reveal a paginated experience and ignore a stale diary', async () => {
  const { model } = harness(async () => response({}));
  const active = { id: 'active', kind: 'experience', content: 'Active' };
  const archived = { id: 'archived', kind: 'experience', archived: true, content: 'Archived' };
  const expired = { id: 'expired', kind: 'experience', expires_at: 1, content: 'Expired' };
  const summary = { experience_ids: ['active', 'archived', 'expired', 'ordinary', 'missing', 'active'] };
  const focus = [];
  model.$refs = { memoryFactsList: { querySelectorAll: () => [
    { dataset: { factId: 'active' }, focus: () => focus.push('active'), scrollIntoView() {} },
    { dataset: { factId: 'archived' }, focus: () => focus.push('archived'), scrollIntoView() {} },
  ] } };
  model.selectMemoryProfile('a');
  model.memoryFacts = [...Array.from({ length: 35 }, (_, index) => ({
    id: 'new-' + index, kind: 'experience', pinned: true,
  })), active, archived, expired, { id: 'ordinary', content: 'Fact' }];
  model.memorySummaries = [summary];
  assert.deepEqual(Array.from(model.memoryDiaryExperiences(summary), item => item.id), ['active', 'archived', 'expired']);
  model.factSearch = 'hides the target';
  model.factFilterEmotional = 'with_note';
  await model.jumpToMemoryExperience(summary, 'active');
  assert.equal(model.memoryActiveSubTab, 'facts');
  assert.equal(model.factFilterStatus, 'experiences');
  assert.ok(model.displayedMemoryFacts.includes(active));
  await model.jumpToMemoryExperience(summary, 'archived');
  assert.equal(model.factFilterStatus, 'archived');
  assert.deepEqual(focus, ['active', 'archived']);
  model.selectMemoryProfile('b');
  model.memoryFacts = [{ ...active }];
  model.memoryActiveSubTab = 'summaries';
  await model.jumpToMemoryExperience(summary, 'active');
  assert.equal(model.memoryActiveSubTab, 'summaries');
  assert.deepEqual(focus, ['active', 'archived']);
});

test('a failed old-store page keeps the prior results and allows retry or an explicit restart', async () => {
  const page = {
    profile_id: 'a', items: [],
    coverage: { total: 2, offset: 0, covered: 1 }, next_cursor: 'opaque:next',
  };
  const calls = [];
  const { model } = harness(async (url, options) => {
    assert.equal(url, 'memoryCurationBulkPreview/a');
    const { cursor } = JSON.parse(options.body);
    calls.push(cursor);
    return cursor === null ? response(page) : response({ detail: 'Preview failed' }, false);
  });
  model.selectMemoryProfile('a');
  await model.loadMemoryBulkCurationPage('a');
  await model.loadMemoryBulkCurationPage('a');
  assert.equal(model.memoryBulkCurationPage, page);
  assert.equal(model.memoryBulkCurationError, true);
  await model.loadMemoryBulkCurationPage('a');
  await model.loadMemoryBulkCurationPage('a', true);
  assert.deepEqual(calls, [null, 'opaque:next', 'opaque:next', null]);
  assert.equal(model.memoryBulkCurationError, false);
});

test('old-store pages and their pending state are discarded when switching personas and back', async () => {
  const pending = [];
  const { model } = harness(() => new Promise(resolve => pending.push(resolve)));
  const payload = fact_id => ({
    profile_id: 'a', items: [{ fact_id, action: 'keep' }],
    coverage: { total: 2, offset: 0, covered: 1 }, next_cursor: 'more',
  });
  model.selectMemoryProfile('a');
  const first = model.loadMemoryBulkCurationPage('a');
  pending[0](response(payload('first')));
  await first;
  const old = model.loadMemoryBulkCurationPage('a');
  model.selectMemoryProfile('b');
  assert.equal(model.memoryBulkCurationPage, null);
  assert.equal(model.memoryBulkCurationPending, false);
  model.selectMemoryProfile('a');
  const current = model.loadMemoryBulkCurationPage('a');
  pending[1](response(payload('old')));
  await old;
  assert.equal(model.memoryBulkCurationPage, null);
  assert.equal(model.memoryBulkCurationPending, true);
  pending[2](response(payload('current')));
  await current;
  assert.equal(model.memoryBulkCurationPage.items[0].fact_id, 'current');
  assert.equal(model.memoryBulkCurationPending, false);
});

test('an old-store preview leaves a normal preview selectable and applicable', async () => {
  const calls = [];
  const { model } = harness(async (url, options) => {
    if (url === 'memoryCurationPreview/a') return response({
      profile_id: 'a', preview_id: 'normal', items: [{ fact_id: 'eligible', action: 'archive' }],
    });
    if (url === 'memoryCurationBulkPreview/a') return response({
      profile_id: 'a', items: [{ fact_id: 'bulk', action: 'review' }],
      coverage: { total: 1, offset: 0, covered: 1 }, next_cursor: null,
    });
    if (url === 'memoryCurationApply/a') {
      calls.push(JSON.parse(options.body));
      return response({ ok: true });
    }
    return response({});
  });
  model.selectMemoryProfile('a');
  await model.loadMemoryCurationPreview('a');
  await model.loadMemoryBulkCurationPage('a');
  assert.deepEqual([...model.memoryCurationSelection], ['eligible']);
  await model.applyMemoryCuration();
  assert.deepEqual(calls, [{ preview_id: 'normal', fact_ids: ['eligible'] }]);
});

test('an empty preview cannot be applied and a rejected preview cannot be applied again', async () => {
  let items = [];
  const writes = [];
  const { model, toasts } = harness(async (url, options) => {
    if (url.startsWith('memoryCurationPreview/')) return response({ profile_id: 'a', preview_id: 'p1', items });
    if (options?.method === 'POST') {
      writes.push([url, JSON.parse(options.body)]);
      return response({ ok: false }, false);
    }
    return response({ facts: [] });
  });
  model.selectMemoryProfile('a');
  await model.loadMemoryCurationPreview('a');
  await model.applyMemoryCuration();
  assert.deepEqual(writes, []);
  items = [{ fact_id: 'one', action: 'keep' }];
  await model.loadMemoryCurationPreview('a');
  await model.applyMemoryCuration();
  await model.applyMemoryCuration();
  assert.deepEqual(writes, [['memoryCurationApply/a', { preview_id: 'p1', fact_ids: [] }]]);
  assert.equal(model.memoryCurationPreview, null);
  assert.deepEqual(toasts.map(toast => toast.type), ['error']);
});

test('only keep suggestions can be confirmed when every changed item is unchecked', async () => {
  const writes = [];
  let items = [{ fact_id: 'one', action: 'review' }];
  const { model } = harness(async (url, options) => {
    if (url.startsWith('memoryCurationPreview/')) return response({ profile_id: 'a', preview_id: 'p1', items });
    if (url.startsWith('memoryCurationApply/')) {
      writes.push(JSON.parse(options.body));
      return response({ ok: true, merged: 0, archived: 0, reviewed: 0 });
    }
    return response({ facts: [] });
  });
  model.selectMemoryProfile('a');
  await model.loadMemoryCurationPreview('a');
  model.memoryCurationSelection = [];
  await model.applyMemoryCuration();
  assert.deepEqual(writes, []);

  items = [...items, { fact_id: 'two', action: 'keep' }];
  await model.loadMemoryCurationPreview('a');
  model.memoryCurationSelection = [];
  await model.applyMemoryCuration();
  assert.deepEqual(writes, [{ preview_id: 'p1', fact_ids: [] }]);
  assert.equal(model.memoryCurationResult.reviewed, 0);
});

test('switching away and back discards an older preview and apply result', async () => {
  let finishPreview;
  let finishApply;
  const { model, toasts } = harness((url) => {
    if (url === 'memoryCurationPreview/a' && !finishPreview) {
      return new Promise(resolve => { finishPreview = resolve; });
    }
    if (url === 'memoryCurationApply/a') return new Promise(resolve => { finishApply = resolve; });
    return Promise.resolve(response({ profile_id: 'a', preview_id: 'fresh', items: [{ fact_id: 'fresh-fact', action: 'review' }] }));
  });
  model.selectMemoryProfile('a');
  const stalePreview = model.loadMemoryCurationPreview('a');
  model.selectMemoryProfile('b');
  model.selectMemoryProfile('a');
  await model.loadMemoryCurationPreview('a');
  finishPreview(response({ profile_id: 'a', preview_id: 'stale', items: [{ fact_id: 'stale-fact', action: 'archive' }] }));
  await stalePreview;
  assert.equal(model.memoryCurationPreview.preview_id, 'fresh');
  assert.deepEqual([...model.memoryCurationSelection], ['fresh-fact']);

  const apply = model.applyMemoryCuration();
  model.selectMemoryProfile('b');
  finishApply(response({ ok: true, reviewed: 1 }));
  await apply;
  assert.equal(model.memoryCurationPreview, null);
  assert.equal(model.memoryCurationResult, null);
  assert.deepEqual(toasts, []);
});

test('restore uses the visible archived fact revision and ignores stale profile responses', async () => {
  const calls = [];
  let finishRestore;
  const { model, toasts } = harness((url, options) => {
    calls.push([url, options]);
    if (url.startsWith('memoryFactRestore/')) return new Promise(resolve => { finishRestore = resolve; });
    return Promise.resolve(response({ facts: [{ id: 'one', archived: false }] }));
  });
  const fact = { id: 'one', kind: 'experience', archived: true, archive_revision: 'revision-a' };
  model.selectMemoryProfile('a');
  model.memoryFacts = [fact, { id: 'legacy', archived: true }];
  await model.restoreMemoryFact(model.memoryFacts[1]);
  const restore = model.restoreMemoryFact(fact);
  await model.restoreMemoryFact(fact);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'memoryFactRestore/a/one');
  assert.equal(calls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0][1].body), { archive_revision: 'revision-a' });
  finishRestore(response({ ok: true }));
  await restore;
  assert.equal(model.memoryFacts[0].archived, false);
  assert.equal(toasts.length, 1);

  model.memoryFacts = [fact];
  const staleRestore = model.restoreMemoryFact(fact);
  model.selectMemoryProfile('b');
  model.memoryFacts = [{ id: 'one', archived: true, archive_revision: 'revision-b' }];
  finishRestore(response({ ok: true }));
  await staleRestore;
  await model.restoreMemoryFact(fact);
  assert.equal(model.memoryFacts[0].archive_revision, 'revision-b');
  assert.equal(calls.filter(([url]) => url.startsWith('memoryFactRestore/')).length, 2);
  assert.equal(toasts.length, 1);
});

test('daily curation round-trips independently of index maintenance in global settings', async () => {
  let stored = { daily_forgetting_enabled: true };
  const writes = [];
  const { model } = harness(async (url, options) => {
    assert.equal(url, 'settingsMemory');
    if (!options) return response(stored);
    assert.equal(options.method, 'POST');
    stored = JSON.parse(options.body);
    writes.push([stored.daily_curation_enabled, stored.daily_forgetting_enabled]);
    return response({ ok: true });
  });
  assert.equal(model.memoryGlobalForm.daily_curation_enabled, false);
  assert.equal(model.memoryGlobalFormLoaded, false);
  await model.loadMemoryGlobalConfig();
  assert.equal(model.memoryGlobalFormLoaded, true);
  assert.equal(model.memoryGlobalForm.daily_curation_enabled, false);
  for (const enabled of [true, false]) {
    model.memoryGlobalForm.daily_curation_enabled = enabled;
    await model.saveMemoryGlobalConfig();
    await model.loadMemoryGlobalConfig();
    assert.equal(model.memoryGlobalForm.daily_curation_enabled, enabled);
  }
  assert.deepEqual(writes, [[true, true], [false, true]]);
});

test('daily curation autosave preserves the inherited extraction preset and absent group override', async () => {
  let stored = {};
  const writes = [];
  const { model, watch, runDebounce } = harness(async (_url, options) => {
    if (!options) return response({ memory_config: stored });
    const body = JSON.parse(options.body);
    writes.push(body);
    stored = { ...stored, ...body };
    return response({ ok: true });
  });
  await model.onMemorySettingsProfileChange('a');
  assert.equal(model.memoryTabProfileForm.extraction_llm_preset, null);
  assert.equal(model.memoryTabProfileForm.group_chat_merge_into_history, null);
  model.memoryTabProfileForm.daily_curation_enabled = true;
  watch.memoryTabProfileForm.handler.call(model);
  runDebounce();
  await tick();
  assert.equal(writes[0].daily_curation_enabled, true);
  assert.equal(writes[0].extraction_llm_preset, null);
  assert.equal(Object.hasOwn(writes[0], 'group_chat_merge_into_history'), false);

  stored = { extraction_llm_preset: '', group_chat_merge_into_history: false };
  await model.loadMemoryTabProfileConfig('a');
  assert.equal(model.memoryTabProfileForm.extraction_llm_preset, '');
  assert.equal(model.memoryTabProfileForm.group_chat_merge_into_history, false);
  model.memoryTabProfileForm.daily_curation_enabled = false;
  await model.saveMemoryTabProfileConfig();
  assert.equal(writes[1].extraction_llm_preset, '');
  assert.equal(writes[1].group_chat_merge_into_history, false);
});

test('persona daily curation round-trips inherit, on and off through its memory config', async () => {
  let stored = { daily_forgetting_enabled: false };
  const writes = [];
  const { model } = harness(async (url, options) => {
    assert.equal(url, 'profileMemoryConfig/a');
    if (!options) return response({ memory_config: stored });
    assert.equal(options.method, 'PUT');
    stored = JSON.parse(options.body);
    writes.push([stored.daily_curation_enabled, stored.daily_forgetting_enabled]);
    return response({ ok: true });
  });
  await model.onMemorySettingsProfileChange('a');
  assert.equal(model.memoryTabProfileForm.daily_curation_enabled, null);
  for (const enabled of [true, false, null]) {
    model.memoryTabProfileForm.daily_curation_enabled = enabled;
    await model.saveMemoryTabProfileConfig();
    await model.loadMemoryTabProfileConfig();
    assert.equal(model.memoryTabProfileForm.daily_curation_enabled, enabled);
  }
  assert.deepEqual(writes, [[true, false], [false, false], [null, false]]);
});

test('a queued curation toggle saves to the edited persona while old config and status reads are discarded', async () => {
  const writes = [];
  let delayA = false;
  let finishConfig;
  let finishStatus;
  const daily = { day: '2026-11-07', status: 'running' };
  const latest = { origin: 'daily', at: 1794000000, counts: { merged: 1, archived: 2, reviewed: 3 } };
  const { model, watch, runDebounce } = harness((url, options) => {
    if (options?.method === 'PUT') {
      writes.push([url, JSON.parse(options.body).daily_curation_enabled]);
      return Promise.resolve(response({ ok: true }));
    }
    if (url === 'profileMemoryConfig/a' && delayA) return new Promise(resolve => { finishConfig = resolve; });
    if (url === 'memoryStatus/a') return new Promise(resolve => { finishStatus = resolve; });
    if (url === 'memoryStatus/b') return Promise.resolve(response({ curation: { latest, daily } }));
    return Promise.resolve(response({ memory_config: { daily_curation_enabled: url.endsWith('/a') } }));
  });

  await model.onMemorySettingsProfileChange('a');
  model.memoryTabProfileForm.daily_curation_enabled = null;
  watch.memoryTabProfileForm.handler.call(model);
  const staleStatus = model.loadMemoryStatus('a');
  delayA = true;
  const staleConfig = model.loadMemoryTabProfileConfig('a');
  await tick();
  await model.onMemorySettingsProfileChange('b');
  await model.loadMemoryStatus('b');
  finishConfig(response({ memory_config: { daily_curation_enabled: true } }));
  finishStatus(response({ curation: { latest: null, daily: { day: '2026-11-06', status: 'failed' } } }));
  await Promise.all([staleConfig, staleStatus]);
  runDebounce();
  await tick();

  assert.deepEqual(writes, [['profileMemoryConfig/a', null]]);
  assert.equal(model.memoryTabFormProfileId, 'b');
  assert.equal(model.memoryTabProfileForm.daily_curation_enabled, false);
  assert.deepEqual(model.memoryStatus.curation, { latest, daily });
  assert.equal(model.memoryCurationResult, null);
  model.selectMemoryProfile('c');
  assert.equal(model.memoryStatus, null);
});

test('vector sync reports incomplete coverage and refreshes the index view', async () => {
  const calls = [];
  const { model } = harness(async url => {
    calls.push(url);
    return response(url.startsWith('memoryVectorsSyncFacts') ? { vector_count: 2, total: 3 } : {});
  });
  model.t = key => key === 'vectorSyncCoverage' ? '{indexed} of {total}' : key;
  model.memoryProfileId = 'a';
  await model.syncFactsToVectors();
  assert.equal(model.syncFactsResult.text, '2 of 3');
  assert.equal(model.syncFactsResult.ok, false);
  assert.deepEqual(calls, ['memoryVectorsSyncFacts/a', 'memoryVectors/a', 'memoryStatus/a']);
});
