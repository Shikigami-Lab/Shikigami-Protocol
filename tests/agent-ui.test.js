const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const helper = path.join(__dirname, '../static/js/agent-ui.js');
const api = fs.existsSync(helper) ? require(helper) : {};
// Use the shipped Vue runtime for proxy regressions; no DOM or server is needed.
const vueSandbox = vm.createContext({ console });
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/libs/vue.global.prod.js'), 'utf8'), vueSandbox);
const realVue = vueSandbox.Vue;
const event = (sequence, type = 'status', extra = {}) => ({
  run_id: 'r1', profile_id: 'p1', session_id: 'p1', sequence, type, status: 'running', ...extra,
});

test('task lifecycle helper is available', () => assert.equal(typeof api.reduceEvent, 'function'));

const flushViewSync = () => new Promise(resolve => setImmediate(resolve));

test('agent view mount syncs a restored group silently and polling skips acknowledged values', async () => {
  const calls = [];
  const { model, sandbox } = harness(async (url, options) => {
    calls.push({ url, method: options.method, body: JSON.parse(options.body) });
    return { ok: true, json: async () => ({ ok: true }) };
  });
  let poll;
  sandbox.setInterval = callback => { poll = callback; return 1; };
  model.currentSessionId = null; model.currentGroupId = 'stored-group';
  model.statusText = 'Group ready';
  model.receiveAgentEvent(event(1, 'started', { origin: 'reflection' }));
  const runs = model.agentRuns;
  sandbox.AgentUIMixin.mounted.call(model);
  await flushViewSync();
  assert.deepEqual(calls, [{ url: 'http://fixture.test/api/agent/view', method: 'POST', body: { group_chat: true } }]);
  poll(); poll();
  await flushViewSync();
  assert.equal(calls.length, 1);
  assert.equal(model.agentRuns, runs, 'view synchronization must not cancel existing tasks');
  assert.equal(model.statusText, 'Group ready');
  assert.equal(model.messages.length, 0);
  assert.equal(model.spoken.length, 0);
  sandbox.AgentUIMixin.unmounted.call(model);
});

test('agent view sends single-chat mount and session changes through the same contract', async () => {
  const calls = [];
  const { model, sandbox } = harness(async (_url, options) => {
    calls.push(JSON.parse(options.body).group_chat);
    return { ok: true, json: async () => ({ ok: true }) };
  });
  sandbox.AgentUIMixin.mounted.call(model);
  await flushViewSync();
  model.currentSessionId = 'p2';
  sandbox.AgentUIMixin.watch.currentSessionId.call(model);
  await flushViewSync();
  assert.deepEqual(calls, [false, false]);
  sandbox.AgentUIMixin.unmounted.call(model);
});

test('agent view honors persisted group selection before asynchronous restoration and clears an invalid saved group on polling', async () => {
  const calls = [];
  let savedGroup = 'saved-group', poll;
  const { model, sandbox } = harness(async (_url, options) => {
    calls.push(JSON.parse(options.body).group_chat);
    return { ok: true, json: async () => ({ ok: true }) };
  });
  sandbox.localStorage.getItem = key => key === 'currentGroupId' ? savedGroup : null;
  sandbox.setInterval = callback => { poll = callback; return 1; };
  model.currentSessionId = null;
  sandbox.AgentUIMixin.mounted.call(model);
  await flushViewSync();
  assert.deepEqual(calls, [true], 'mount must sync saved group before app.js restores it');
  model.currentSessionId = 'p1';
  sandbox.AgentUIMixin.watch.currentSessionId.call(model);
  await flushViewSync();
  assert.deepEqual(calls, [true, true], 'loading the backend profile must not unblock admissions during group restoration');
  savedGroup = null; // app.js removed a saved group which no longer exists
  poll();
  await flushViewSync();
  assert.deepEqual(calls, [true, true, false]);
  sandbox.AgentUIMixin.unmounted.call(model);
});

test('agent view serializes group-to-single changes behind the old request', async () => {
  const calls = [], pending = [];
  let serverGroup = false;
  const { model, sandbox } = harness(async (_url, options) => {
    const value = JSON.parse(options.body).group_chat;
    calls.push(value);
    await new Promise(resolve => pending.push(resolve));
    serverGroup = value;
    return { ok: true, json: async () => ({ ok: true }) };
  }, { reactive: true });
  model.currentGroupId = 'g1';
  sandbox.AgentUIMixin.watch.currentGroupId.call(model);
  model.currentGroupId = null; model.currentSessionId = 'p2';
  sandbox.AgentUIMixin.watch.currentGroupId.call(model);
  sandbox.AgentUIMixin.watch.currentSessionId.call(model);
  assert.deepEqual(calls, [true]);
  pending.shift()();
  await flushViewSync();
  assert.deepEqual(calls, [true, false]);
  assert.equal(serverGroup, true);
  pending.shift()();
  await flushViewSync();
  assert.equal(serverGroup, false);
});

test('agent view skips superseded pending groups and ignores a stale successful value', async () => {
  const calls = [], pending = [];
  const { model, sandbox } = harness(async (_url, options) => {
    calls.push(JSON.parse(options.body).group_chat);
    await new Promise(resolve => pending.push(resolve));
    return { ok: true, json: async () => ({ ok: true }) };
  });
  sandbox.AgentUIMixin.mounted.call(model); // single-chat request remains in flight
  model.currentGroupId = 'g1';
  sandbox.AgentUIMixin.watch.currentGroupId.call(model);
  model.currentGroupId = null;
  sandbox.AgentUIMixin.watch.currentGroupId.call(model);
  pending.shift()();
  await flushViewSync();
  assert.deepEqual(calls, [false], 'the obsolete pending group value must never be sent');
  model.currentGroupId = 'g2';
  sandbox.AgentUIMixin.watch.currentGroupId.call(model);
  assert.deepEqual(calls, [false, true]);
  pending.shift()();
  await flushViewSync();
  sandbox.AgentUIMixin.unmounted.call(model);
});

test('agent view failures stay silent and retry only on polling or a view change', async () => {
  for (const failure of ['network', 'http']) {
    const calls = [];
    let fail = true, poll;
    const { model, sandbox } = harness(async (_url, options) => {
      calls.push(JSON.parse(options.body).group_chat);
      if (fail && failure === 'network') throw new Error('offline');
      return { ok: !fail, status: 503, json: async () => fail ? { detail: 'unavailable' } : { ok: true } };
    });
    model.currentGroupId = 'g1'; model.statusText = 'Group ready';
    sandbox.setInterval = callback => { poll = callback; return 1; };
    sandbox.AgentUIMixin.mounted.call(model);
    await flushViewSync(); await flushViewSync();
    assert.deepEqual(calls, [true], 'failure must not spin retries');
    poll();
    await flushViewSync();
    assert.deepEqual(calls, [true, true]);
    fail = false;
    model.currentGroupId = null;
    sandbox.AgentUIMixin.watch.currentGroupId.call(model);
    await flushViewSync();
    assert.deepEqual(calls, [true, true, false]);
    poll();
    await flushViewSync();
    assert.equal(calls.length, 3);
    assert.equal(model.statusText, 'Group ready');
    assert.equal(model.agentRunErrors.p1, undefined);
    sandbox.AgentUIMixin.unmounted.call(model);
  }
});

test('agent view retries the latest value after an uncertain failed stale request', async () => {
  const calls = [];
  let rejectGroup;
  const { model, sandbox } = harness(async (_url, options) => {
    const value = JSON.parse(options.body).group_chat;
    calls.push(value);
    if (value) await new Promise((_resolve, reject) => { rejectGroup = reject; });
    return { ok: true, json: async () => ({ ok: true }) };
  });
  sandbox.AgentUIMixin.mounted.call(model);
  await flushViewSync(); // confirmed single chat
  model.currentGroupId = 'g1';
  sandbox.AgentUIMixin.watch.currentGroupId.call(model);
  model.currentGroupId = null;
  sandbox.AgentUIMixin.watch.currentGroupId.call(model);
  rejectGroup(new Error('response lost after server applied group view'));
  await flushViewSync();
  assert.deepEqual(calls, [false, true, false], 'old single-chat acknowledgement cannot survive an uncertain group request');
  sandbox.AgentUIMixin.unmounted.call(model);
});

test('agent view unmount prevents late responses from sending queued updates', async () => {
  const calls = [];
  let finish;
  const { model, sandbox } = harness(async (_url, options) => {
    calls.push(JSON.parse(options.body).group_chat);
    await new Promise(resolve => { finish = resolve; });
    return { ok: true, json: async () => ({ ok: true }) };
  });
  sandbox.AgentUIMixin.mounted.call(model);
  model.currentGroupId = 'g1';
  sandbox.AgentUIMixin.watch.currentGroupId.call(model);
  sandbox.AgentUIMixin.unmounted.call(model);
  finish();
  await flushViewSync();
  assert.deepEqual(calls, [false]);
});

test('reflection snapshots stay in the drawer without message ownership or continuation', async () => {
  const { model, sandbox } = harness();
  model.messages = [{ id: 10, role: 'user', session_id: 'p1' },
    { id: 11, role: 'assistant', session_id: 'p1', agent_run_id: 'r1' }];
  model.agentFetch = async () => ({ runs: [{ id: 'r1', profile_id: 'p1', session_id: 'p1',
    origin: 'reflection', state: 'completed', user_message_id: 10, assistant_message_id: 11,
    events: [event(1, 'started'), event(2, 'completed')] }] });
  await sandbox.AgentUIMixin.methods.refreshAgentRuns.call(model);
  const run = model.currentAgentRuns[0];
  assert.ok(run, 'even a reflection task with no tool calls is visible');
  assert.equal(run.origin, 'reflection');
  assert.match(model.agentRunLabel(run), /Reflection/);
  assert.equal(model.agentMessageOwner(run), null);
  assert.equal(model.messageAgentRuns(model.messages[1]).length, 0);
  assert.equal(model.agentCanContinueRun(run), false);
  model.selectAgentContinuation(run);
  model.continueAgentMessage({ ...model.messages[1], generation_status: 'completed' });
  assert.equal(model.agentContinueRunId, '');
  assert.equal(model.messages.length, 2);
  assert.equal(model.spoken.length, 0);
});

test('reflection event origin survives later events and never binds to a user stream', () => {
  const { model } = harness();
  const context = { sessionId: 'p1', viewVersion: 0, requestId: 'client' };
  model.agentChatContext = context; model.isStreaming = true; model.statusText = 'User reply';
  model.receiveAgentEvent(event(1, 'started', { origin: 'reflection', request_id: 'client' }), context);
  model.receiveAgentEvent(event(2, 'completed'));
  const run = Object.values(model.agentRuns)[0];
  assert.equal(run.origin, 'reflection');
  assert.equal(context.runKey, undefined);
  assert.equal(model.agentRunIsAttached(run), false);
  assert.equal(model.statusText, 'User reply');
});

test('continuation waits for a confirmed user origin before passing a task to chat', () => {
  const { model } = harness();
  const run = { id: 'r1', profile_id: 'p1', session_id: 'p1', state: 'completed' };
  const message = { agent_run_id: 'r1', session_id: 'p1', generation_status: 'completed' };
  model.agentRuns = { [api.keyOf(run)]: run };
  model.selectAgentContinuation(run); model.continueAgentMessage(message);
  assert.equal(model.agentCanContinueRun(run), false);
  assert.equal(model.agentContinueRunId, '');
  model.agentRuns[api.keyOf(run)] = { ...run, origin: 'user' };
  model.continueAgentMessage(message);
  assert.equal(model.agentContinueRunId, 'r1');
});

test('late reflection polling remains scoped after profile and group switches', async () => {
  let resolve;
  const { model, sandbox } = harness();
  model.agentFetch = () => new Promise(r => { resolve = r; });
  const polling = sandbox.AgentUIMixin.methods.refreshAgentRuns.call(model);
  model.currentSessionId = 'p2'; model.currentGroupId = 'g1'; model.agentViewChanged();
  model.messages = [{ role: 'user', content: 'Group chat', session_id: 'group:g1' }];
  model.statusText = 'Group ready';
  resolve({ runs: [{ id: 'r1', profile_id: 'p1', session_id: 'p1', origin: 'reflection',
    state: 'approval_requested', events: [event(1, 'approval_requested', { approval_id: 'a1' })] }] });
  await polling;
  assert.equal(model.currentAgentRuns.length, 0);
  assert.equal(model.agentActiveTaskCount, 0);
  assert.equal(model.agentOwnTaskActive, false);
  assert.equal(model.messages.length, 1);
  assert.equal(model.statusText, 'Group ready');
  assert.equal(model.spoken.length, 0);
  model.currentGroupId = null;
  assert.equal(model.currentAgentRuns.length, 0);
  model.currentSessionId = 'p1';
  assert.equal(model.currentAgentRuns[0].origin, 'reflection');
  assert.equal(model.currentAgentRuns[0].approval.approval_id, 'a1');
});

test('autonomy settings default off with contract defaults, including older responses', async () => {
  const { model } = harness();
  for (const form of [model.agentSettingsForm]) {
    assert.equal(form.autonomy_enabled, false);
    assert.equal(form.autonomy_interval_seconds, 900);
    assert.equal(form.autonomy_max_requests_per_hour, 12);
  }
  model.agentFetch = async () => ({ enabled: true });
  await model.loadAgentSettings();
  assert.equal(model.agentSettingsForm.autonomy_enabled, false);
  assert.equal(model.agentSettingsForm.autonomy_interval_seconds, 900);
  assert.equal(model.agentSettingsForm.autonomy_max_requests_per_hour, 12);
});

test('autonomy validates integer ranges without making requests and saves valid limits', async () => {
  const { model } = harness();
  model.agentSettingsLoaded = true;
  const calls = [];
  model.agentFetch = async (url, options) => {
    calls.push({ url, options });
    return options ? JSON.parse(options.body) : {};
  };
  for (const patch of [{ autonomy_interval_seconds: 59 }, { autonomy_interval_seconds: 86401 },
    { autonomy_interval_seconds: 60.5 }, { autonomy_interval_seconds: '900' },
    { autonomy_max_requests_per_hour: 0 }, { autonomy_max_requests_per_hour: 129 },
    { autonomy_max_requests_per_hour: NaN }, { autonomy_enabled: 'true' }]) {
    await model.saveAgentSettings(patch);
    assert.equal(calls.length, 0);
    assert.ok(model.agentSettingsError);
  }
  for (const [interval, cap] of [[60, 1], [86400, 128]]) {
    const patch = { autonomy_enabled: true, autonomy_interval_seconds: interval, autonomy_max_requests_per_hour: cap };
    await model.saveAgentSettings(patch);
    assert.deepEqual(JSON.parse(calls.at(-2).options.body), patch);
    assert.equal(model.agentSettingsForm.autonomy_max_requests_per_hour, cap);
  }
});

test('character autonomy requires explicit opt-in and PATCH stays profile scoped', async () => {
  const calls = [];
  const { model } = harness(async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    return { ok: true, json: async () => ({ ok: true }) };
  });
  model.profiles = [{ profile_id: 'p1', tool_configs: { agent: { autonomy_enabled: true } } },
    { profile_id: 'p2', tool_configs: { agent: {} } }];
  model.selectedProfileId = 'p1';
  assert.equal(model.getProfileAgentPermission('autonomy_enabled'), true);
  model.selectedProfileId = 'p2';
  assert.equal(model.getProfileAgentPermission('autonomy_enabled'), false);
  model.loadProfiles = model.loadToolsConfig = async () => {};
  await model.saveToolConfig('agent', { autonomy_enabled: true }, 'p2');
  assert.match(calls[0].url, /\/p2\/tool_configs$/);
  assert.deepEqual(calls[0].body, { tool_id: 'agent', config: { autonomy_enabled: true } });
});

test('older profile list cannot overwrite newly loaded character permissions', async () => {
  const pending = [];
  const { model } = harness(() => new Promise(resolve => pending.push(resolve)));
  model.selectedProfileId = 'p1';
  const first = model.loadProfiles();
  model.selectedProfileId = 'p2';
  const second = model.loadProfiles();
  pending[1]({ ok: true, json: async () => ({ profiles: [{ profile_id: 'p2', tool_configs: { agent: { autonomy_enabled: false } } }] }) });
  await second;
  pending[0]({ ok: true, json: async () => ({ profiles: [{ profile_id: 'p2', tool_configs: { agent: { autonomy_enabled: true } } }] }) });
  await first;
  assert.equal(model.getProfileAgentPermission('autonomy_enabled'), false);
});

test('profile list started before a switch is ignored even without another list request', async () => {
  let resolve;
  const { model } = harness(() => new Promise(r => { resolve = r; }));
  model.selectedProfileId = 'p1';
  const loading = model.loadProfiles();
  model.selectedProfileId = 'p2';
  model.profiles = [{ profile_id: 'p2', tool_configs: { agent: { autonomy_enabled: false } } }];
  resolve({ ok: true, json: async () => ({ profiles: [] }) });
  await loading;
  assert.equal(model.profiles.length, 1);
});

test('settings selection invalidates pending profile and tool loads across A-B-A', async () => {
  const pending = [];
  const { model } = harness(() => new Promise(resolve => pending.push(resolve)));
  model.selectedProfileId = 'p1'; model.activeTab = 'profiles'; model.profiles = [];
  model.loadTrendStatus = model.loadWeatherStatus = async () => {};
  const profiles = model.loadProfiles(), tools = model.loadToolsConfig('p1');
  model.selectProfile('p2'); model.selectProfile('p1');
  model.profiles = [{ profile_id: 'p1', tool_configs: { agent: { autonomy_enabled: false } } }];
  model.toolsList = [{ tool_id: 'current' }];
  pending[0]({ ok: true, json: async () => ({ profiles: [] }) });
  pending[1]({ ok: true, json: async () => ({ tools: [{ tool_id: 'stale' }] }) });
  await Promise.all([profiles, tools]);
  assert.equal(model.profiles.length, 1);
  assert.equal(model.toolsList[0].tool_id, 'current');
});

test('group send remains on the group endpoint without autonomous task continuation or tools', async () => {
  const calls = [];
  const { model } = harness(async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    return streamResponse([{ agent_event: event(1, 'started', { origin: 'reflection' }) }, { done: true }]);
  });
  model.currentGroupId = 'g1'; model.agentContinueRunId = 'autonomous-task';
  model.agentSettingsForm.autonomy_enabled = true;
  await model.sendMessage();
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /\/groups\/g1\/chat$/);
  assert.deepEqual(Object.keys(calls[0].body).sort(), ['client_id', 'message']);
  assert.equal(Object.keys(model.agentRuns).length, 0);
  assert.equal(model.currentAgentRuns.length, 0);
});

test('late tool-load and save errors are silent after a character switch', async () => {
  for (const action of ['load', 'save']) {
    let reject;
    const { model } = harness(() => new Promise((_, r) => { reject = r; }));
    model.selectedProfileId = 'p1'; model.activeTab = 'profiles';
    model.toolsList = [{ tool_id: 'new-profile-tool' }];
    const toasts = []; model.showToast = (...args) => toasts.push(args);
    const loading = action === 'load' ? model.loadToolsConfig('p1') : model.saveToolConfig('agent', { autonomy_enabled: true });
    model.selectedProfileId = 'p2';
    reject(new Error('old profile error'));
    await loading;
    assert.equal(model.toolsList[0]?.tool_id, 'new-profile-tool');
    assert.equal(toasts.length, 0);
  }
});

test('tool status responses cannot overwrite another character after switching away and back', async () => {
  for (const [method, field] of [['loadTrendStatus', 'trendStatus'], ['loadWeatherStatus', 'weatherStatus']]) {
    let resolve;
    const { model } = harness(() => new Promise(r => { resolve = r; }));
    model.selectedProfileId = 'p1'; model.profiles = [];
    const loading = model[method]('p1');
    model.selectProfile('p2'); model.selectProfile('p1');
    model[field] = { current: true };
    resolve({ ok: true, json: async () => ({ stale: true }) });
    await loading;
    assert.equal(model[field].current, true);
  }
});

test('autonomy controls reuse settings and continuation controls use the origin guard', () => {
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  assert.match(html, /saveAgentSettings\(\{autonomy_enabled: \$event\}\)/);
  assert.match(html, /getProfileAgentPermission\('autonomy_enabled'\)/);
  assert.match(html, /id="agent-autonomy-interval"[^>]*min="60"[^>]*max="86400"/);
  assert.match(html, /id="agent-autonomy-hourly-cap"[^>]*min="1"[^>]*max="128"/);
  assert.equal((html.match(/:can-continue="agentCanContinueRun\(run\)"/g) || []).length, 2);
});
test('duplicate and out-of-order events cannot regress a run', () => {
  let runs = api.reduceEvent({}, event(1, 'started'));
  runs = api.reduceEvent(runs, event(3, 'tool_call', { call_id: 'c1', tool_name: 'timer', arguments: '{}' }));
  assert.equal(api.reduceEvent(runs, event(3, 'tool_call')), runs);
  assert.equal(api.reduceEvent(runs, event(2)), runs);
  assert.equal(Object.values(runs)[0].events.length, 2);
});
test('same run ID in other profiles stays isolated and session filtering is exact', () => {
  let runs = api.reduceEvent({}, event(1, 'started'));
  runs = api.reduceEvent(runs, event(1, 'started', { profile_id: 'p2', session_id: 's2' }));
  assert.equal(api.visibleRuns(runs, 'p1', 'p1').length, 1);
  assert.equal(api.visibleRuns(runs, 'p2', 'p1').length, 0);
  assert.equal(api.visibleRuns(runs, 'p2', 's2')[0].profile_id, 'p2');
  assert.equal(api.reduceEvent(runs, { type: 'started', run_id: 'r2' }), runs);
});
test('terminal cancellation survives late status, tool results, and completion', () => {
  let runs = api.reduceEvent({}, event(1, 'started'));
  runs = api.reduceEvent(runs, event(2, 'cancelled'));
  for (const type of ['status', 'completed', 'tool_result']) {
    assert.equal(api.reduceEvent(runs, event(3, type)), runs);
  }
  assert.equal(Object.values(runs)[0].state, 'cancelled');
});
test('ordinary tool results do not become task completion or approval requests', () => {
  let runs = api.reduceEvent({}, event(1, 'tool_result', { call_id: 'c1', is_error: true, content: [{ type: 'text', text: 'bad timer' }] }));
  const run = Object.values(runs)[0];
  assert.equal(run.state, 'running');
  assert.equal(run.approval, null);
  assert.equal(run.events[0].is_error, true);
});
test('approval appears only for an explicit request and clears at terminal state', () => {
  let runs = api.reduceEvent({}, event(1, 'approval_requested', { approval_id: 'a1', action: 'timer' }));
  assert.equal(Object.values(runs)[0].approval.approval_id, 'a1');
  runs = api.reduceEvent(runs, event(2, 'failed'));
  assert.equal(Object.values(runs)[0].approval, null);
});

function harness(fetchImpl = async () => ({ ok: true, json: async () => ({}) }), { reactive = false } = {}) {
  let app;
  const sandbox = { console, AbortController, DOMException, TextDecoder, Uint8Array,
    crypto: require('node:crypto').webcrypto, fetch: fetchImpl,
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: fn => fn(),
    localStorage: { getItem: () => null, setItem() {} },
    location: { origin: 'http://fixture.test', protocol: 'http:', hostname: 'fixture.test' },
    document: { addEventListener() {} }, marked: { setOptions() {} },
    Vue: { createApp(value) { app = value; return { component() {}, mount() {} }; } },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  for (const file of ['api-paths.js', 'i18n.js', 'agent-ui.js', 'settings.js', 'app.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js', file), 'utf8'), sandbox);
  }
  const rawModel = Object.assign({}, ...app.mixins.map(m => m.data()), {
    currentSessionId: 'p1', currentGroupId: null, sessions: [{ id: 'p1', display_name: 'One' }],
    inputText: 'hello', messages: [], streamingText: '', isStreaming: false, locale: 'en',
    ttsEnabled: true, spoken: [], pendingScreenshot: null,
  });
  const model = reactive ? realVue.reactive(rawModel) : rawModel;
  for (const methods of [...app.mixins.map(m => m.methods), app.methods]) {
    for (const [name, method] of Object.entries(methods)) model[name] = method.bind(model);
  }
  for (const [name, getter] of Object.entries(sandbox.AgentUIMixin.computed)) {
    Object.defineProperty(model, name, { get: () => getter.call(model) });
  }
  model.$nextTick = async fn => { if (fn) fn(); };
  model.scrollToBottom = () => {};
  model.sendToTTS = value => model.spoken.push(value);
  model.refreshAgentRuns = async () => {};
  model.refreshTimers = async () => {};
  model.showToast = () => {};
  return { model, sandbox, app };
}
const streamResponse = frames => {
  let sent = false;
  return { ok: true, body: { getReader: () => ({ read: async () => {
    if (sent) return { done: true };
    sent = true;
    return { done: false, value: new TextEncoder().encode(frames.map(f => 'data: ' + JSON.stringify(f) + '\n\n').join('')) };
  } }) } };
};

test('stop POSTs native cancellation before abort; accepted is still pending', async () => {
  const { model } = harness();
  model.receiveAgentEvent(event(1, 'started'));
  const key = api.keyOf(event(1));
  const actions = [];
  let resolvePost;
  model.agentFetch = (url, options) => {
    actions.push('post');
    assert.match(url, /\/r1\/cancel$/);
    assert.equal(JSON.parse(options.body).profile_id, 'p1');
    return new Promise(resolve => { resolvePost = resolve; });
  };
  model.agentChatContext = { sessionId: 'p1', viewVersion: 0, runKey: key, requestId: 'stop-request-1' };
  model.isStreaming = true;
  model.statusText = 'Thinking';
  model._chatAbortController = { abort: () => actions.push('abort') };
  const pending = model.abortChat();
  assert.deepEqual(actions, ['post']);
  assert.equal(model.agentRuns[key].cancelling, true);
  assert.equal(model.statusText, 'Cancelling…');
  resolvePost({ accepted: true, state: 'cancelling' });
  await pending;
  assert.deepEqual(actions, ['post', 'abort']);
  assert.equal(api.terminal(model.agentRuns[key].state), false);
  model.receiveAgentEvent(event(2, 'cancelled'));
  assert.equal(model.agentRuns[key].state, 'cancelled');
  assert.equal(model.statusText, 'Task stopped');
});
test('failed cancellation keeps the stream and allows retry', async () => {
  const { model } = harness();
  model.receiveAgentEvent(event(1, 'started'));
  const key = api.keyOf(event(1));
  let aborted = false;
  model.agentFetch = async () => { throw new Error('offline'); };
  await model.cancelAgentRun(model.agentRuns[key], { abort: () => { aborted = true; } });
  assert.equal(aborted, false);
  assert.equal(model.agentRuns[key].cancelling, false);
  assert.equal(model.agentRuns[key].error, 'offline');
});
test('late cancel acknowledgment cannot overwrite completed run', async () => {
  const { model } = harness();
  model.receiveAgentEvent(event(1, 'started'));
  const key = api.keyOf(event(1));
  model.agentFetch = async () => {
    model.receiveAgentEvent(event(2, 'completed'));
    return { accepted: true, state: 'cancelled' };
  };
  await model.cancelAgentRun(model.agentRuns[key]);
  assert.equal(model.agentRuns[key].state, 'completed');
});
test('ordinary reply, message IDs and TTS exclude tool event contents', async () => {
  const { model } = harness(async () => streamResponse([
    { message_role: 'user', message_id: 12 }, { agent_event: event(1, 'started') },
    { agent_event: event(2, 'tool_result', { content: [{ type: 'text', text: 'PRIVATE TOOL DETAIL' }] }) },
    { token: 'Hello' }, { agent_event: event(3, 'completed') }, { done: true, message_id: 13 },
  ]));
  await model.sendMessage();
  assert.equal(model.messages.length, 2);
  assert.equal(model.messages[0].id, 12);
  assert.equal(model.messages[1].id, 13);
  assert.equal(model.messages[1].content, 'Hello');
  assert.deepEqual(model.spoken, ['Hello']);
  assert.equal(model.isStreaming, false);
});
test('late stream after profile change cannot modify the new chat or speak', async () => {
  let model;
  ({ model } = harness(async () => {
    model.currentSessionId = 'p2';
    model.agentViewChanged();
    model.messages = [{ role: 'user', content: 'New chat' }];
    model.statusText = 'New chat ready';
    return streamResponse([{ agent_event: event(1, 'started') }, { token: 'Old reply' }, { done: true }]);
  }));
  await model.sendMessage();
  assert.equal(model.messages.length, 1);
  assert.equal(model.messages[0].content, 'New chat');
  assert.equal(model.statusText, 'New chat ready');
  assert.equal(model.spoken.length, 0);
  assert.equal(api.visibleRuns(model.agentRuns, 'p1', 'p1').length, 1);
  assert.equal(api.visibleRuns(model.agentRuns, 'p2', 'p2').length, 0);
});
test('image context still reaches ordinary chat and pending image is cleared', async () => {
  const requests = [];
  const { model } = harness(async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    if (url.endsWith('/vlm')) return { ok: true, json: async () => ({ description: 'a cat' }) };
    return streamResponse([{ token: 'Nice cat' }, { done: true }]);
  });
  model.pendingScreenshot = 'data:image/png;base64,ZmFrZQ==';
  model.pendingImageType = 'paste';
  await model.sendMessage();
  const vlm = requests.find(r => r.url.endsWith('/vlm'));
  assert.equal(vlm.body.session_id, 'p1');
  assert.equal(vlm.body.image_type, 'paste');
  assert.match(requests.find(r => r.url.endsWith('/chat')).body.message, /a cat/);
  assert.equal(model.pendingScreenshot, null);
  assert.equal(model.messages[1].content, 'Nice cat');
});
test('view change during image processing does not send chat to another profile', async () => {
  let model, chatSent = false;
  ({ model } = harness(async url => {
    if (url.endsWith('/vlm')) {
      model.currentSessionId = 'p2'; model.agentViewChanged();
      return { ok: true, json: async () => ({ description: 'old image' }) };
    }
    if (url.endsWith('/chat')) chatSent = true;
    return streamResponse([]);
  }));
  model.pendingScreenshot = 'data:image/png;base64,ZmFrZQ==';
  await model.sendMessage();
  assert.equal(chatSent, false);
});
test('stale tools response cannot overwrite the newly selected profile', async () => {
  const pending = [];
  const { model } = harness(() => new Promise(resolve => pending.push(resolve)));
  model.selectedProfileId = 'p1'; model.activeTab = 'profiles';
  model.loadTrendStatus = model.loadWeatherStatus = async () => {};
  const first = model.loadToolsConfig('p1');
  model.selectedProfileId = 'p2';
  const second = model.loadToolsConfig('p2');
  pending[1]({ ok: true, json: async () => ({ tools: [{ tool_id: 'timer', config: { enabled: false } }] }) });
  await second;
  pending[0]({ ok: true, json: async () => ({ tools: [{ tool_id: 'timer', config: { enabled: true } }] }) });
  await first;
  assert.equal(model.toolsList[0].config.enabled, false);
});
test('poll snapshot in flight cannot overwrite a newer terminal event', async () => {
  const { model, sandbox } = harness();
  model.refreshAgentRuns = sandbox.AgentUIMixin.methods.refreshAgentRuns.bind(model);
  model.receiveAgentEvent(event(1, 'started'));
  model.agentFetch = async () => {
    model.receiveAgentEvent(event(3, 'cancelled'));
    return { runs: [{ ...event(1), id: 'r1', state: 'running', events: [event(1, 'started'), event(2)] }] };
  };
  await model.refreshAgentRuns();
  assert.equal(model.agentRuns[api.keyOf(event(1))].state, 'cancelled');
});

test('ordinary chat works with tools disabled and without task events', async () => {
  const { model } = harness(async () => streamResponse([{ token: 'Ordinary reply' }, { done: true, message_id: 4 }]));
  model.agentSettingsForm.enabled = false;
  await model.sendMessage();
  assert.equal(model.messages[1].content, 'Ordinary reply');
  assert.equal(Object.keys(model.agentRuns).length, 0);
  assert.deepEqual(model.spoken, ['Ordinary reply']);
});
test('chat deletion remains scoped and does not delete task activity', () => {
  const { model } = harness();
  model.messages = [{ id: 1, role: 'user', content: 'one' }, { id: 2, role: 'assistant', content: 'two' }];
  model.receiveAgentEvent(event(1, 'started'));
  model._applyMessageDeleted(2, 'p2');
  assert.equal(model.messages.length, 2);
  model._applyMessageDeleted(2, 'p1');
  assert.equal(model.messages.length, 1);
  assert.equal(Object.values(model.agentRuns).length, 1);
});
test('returning to the same profile does not revive a detached stream', () => {
  const { model } = harness();
  const context = { sessionId: 'p1', viewVersion: 0, runKey: null, requestId: 'view-request-1' };
  model.agentChatContext = context;
  model.currentSessionId = 'p2'; model.agentViewChanged();
  model.currentSessionId = 'p1'; model.agentViewChanged();
  assert.equal(model.agentChatIsVisible(context), false);
});
test('timer permission uses existing per-profile PATCH shape', async () => {
  let sent;
  const { model } = harness(async (url, options) => {
    sent = { url, method: options.method, body: JSON.parse(options.body) };
    return { ok: true, json: async () => ({ ok: true }) };
  });
  model.selectedProfileId = 'p2';
  model.loadToolsConfig = async () => {};
  await model.saveToolConfig('timer', { enabled: false }, 'p2');
  assert.equal(sent.url, 'http://fixture.test/api/profiles/p2/tool_configs');
  assert.equal(sent.method, 'PATCH');
  assert.deepEqual(sent.body, { tool_id: 'timer', config: { enabled: false } });
});
test('explicit approval sends the origin profile and is never a blanket permission', async () => {
  const { model } = harness();
  model.receiveAgentEvent(event(1, 'approval_requested', { approval_id: 'a1', action: 'timer' }));
  const key = api.keyOf(event(1));
  model.currentSessionId = 'p2'; model.agentViewChanged();
  let sent;
  model.agentFetch = async (url, options) => {
    sent = { url, body: JSON.parse(options.body) }; return { accepted: true };
  };
  await model.answerAgentApproval(model.agentRuns[key], 'allowed-once');
  assert.match(sent.url, /\/r1\/approval$/);
  assert.deepEqual(sent.body, { profile_id: 'p1', approval_id: 'a1', outcome: 'allowed-once' });
  assert.equal(model.agentRuns[key].approval, null);
});

test('an obsolete SSE connection cannot append replies after returning to a profile', () => {
  const { model, sandbox } = harness();
  sandbox.EventSource = class { constructor() { this.readyState = 1; } close() { this.readyState = 2; } };
  model.subscribeEvents('p1');
  const oldSource = model._sseSource;
  model.currentSessionId = 'p2'; model.subscribeEvents('p2');
  model.currentSessionId = 'p1'; model.subscribeEvents('p1');
  oldSource.onmessage({ data: JSON.stringify({ type: 'new_message', role: 'assistant', content: 'Old connection reply' }) });
  assert.equal(model.messages.length, 0);
});

test('activity hides no-tool successes but retains runs for stop ownership and status', async () => {
  const { model } = harness(async () => streamResponse([
    { agent_event: event(1, 'started') }, { token: 'Hello' }, { agent_event: event(2, 'completed') }, { done: true },
  ]));
  await model.sendMessage();
  assert.equal(model.currentAgentRuns.length, 0);
  assert.equal(model.agentActiveTaskCount, 0);
  assert.equal(model.agentRuns[model.agentChatContext.runKey].state, 'completed');
  model.receiveAgentEvent(event(1, 'started', { run_id: 'r2' }), model.agentChatContext);
  let stopped = false;
  model._chatAbortController = { abort() { stopped = true; } };
  model.agentFetch = async () => ({ accepted: true, state: 'cancelling' });
  await model.abortChat();
  assert.equal(stopped, true);
  assert.equal(model.currentAgentRuns.length, 1); // No attached stream remains; pending cancellation needs controls.
  assert.equal(model.statusText, 'Cancelling…');
  model.receiveAgentEvent(event(2, 'cancelled', { run_id: 'r2' }), model.agentChatContext);
  assert.equal(model.statusText, 'Task stopped');
  assert.equal(model.currentAgentRuns.length, 0);
});

test('activity shows tool calls, explicit approvals, and failures even without tools', () => {
  const { model } = harness();
  model.receiveAgentEvent(event(1, 'failed', { run_id: 'failed' }));
  model.receiveAgentEvent(event(1, 'tool_call', { run_id: 'tool', tool_name: 'timer' }));
  model.receiveAgentEvent(event(2, 'completed', { run_id: 'tool' }));
  model.receiveAgentEvent(event(1, 'approval_requested', { run_id: 'approval', approval_id: 'a1' }));
  model.receiveAgentEvent(event(1, 'started', { run_id: 'ordinary' }));
  model.receiveAgentEvent(event(2, 'completed', { run_id: 'ordinary' }));
  assert.deepEqual(Array.from(model.currentAgentRuns, run => run.run_id).sort(), ['approval', 'failed', 'tool']);
  assert.equal(model.currentAgentRuns.find(run => !model.agentTerminal(run.state)).run_id, 'approval');
  model.currentSessionId = 'p2';
  assert.equal(model.currentAgentRuns.length, 0);
});

test('each send carries one stable request UUID through image preparation and streaming', async () => {
  let model, pendingChat, imageRequestId;
  const sent = [];
  ({ model } = harness(async (url, options) => {
    if (url.endsWith('/vlm')) {
      imageRequestId = model.agentChatContext.requestId;
      return { ok: true, json: async () => ({ description: 'cat' }) };
    }
    if (!url.endsWith('/chat')) return { ok: true };
    sent.push(JSON.parse(options.body));
    if (sent.length === 1) return new Promise(resolve => { pendingChat = resolve; });
    return streamResponse([{ token: 'Second reply' }, { done: true }]);
  }));
  model.pendingScreenshot = 'data:image/png;base64,ZmFrZQ==';
  const first = model.sendMessage();
  while (!pendingChat) await new Promise(resolve => setImmediate(resolve));
  assert.match(sent[0].request_id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  assert.equal(sent[0].request_id, imageRequestId);
  assert.equal(sent[0].request_id, model.agentChatContext.requestId);
  model.inputText = 'duplicate while pending';
  await model.sendMessage();
  assert.equal(sent.length, 1);
  assert.equal(model.agentChatContext.requestId, imageRequestId);
  pendingChat(streamResponse([{ token: 'First reply' }, { done: true }]));
  await first;
  model.inputText = 'next user turn';
  await model.sendMessage();
  assert.notEqual(sent[1].request_id, sent[0].request_id);
});

test('LAN HTTP without randomUUID still sends a cryptographic v4 request ID', async () => {
  let requestId;
  const { model, sandbox } = harness(async (url, options) => {
    requestId = JSON.parse(options.body).request_id;
    return streamResponse([{ token: 'LAN reply' }, { done: true }]);
  });
  sandbox.crypto = { getRandomValues: array => require('node:crypto').webcrypto.getRandomValues(array) };
  await model.sendMessage();
  assert.match(requestId, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  assert.equal(requestId, model.agentChatContext.requestId);
  assert.equal(model.messages[1].content, 'LAN reply');
});

test('runtime limits reject out-of-range and fractional values before any request', async () => {
  for (const patch of [
    { max_model_requests: 0 }, { max_model_requests: 33 }, { max_model_requests: 1.5 },
    { timeout_seconds: 9 }, { timeout_seconds: 601 }, { timeout_seconds: 10.5 },
    { max_model_requests: 8, timeout_seconds: 1 },
  ]) {
    const { model } = harness();
    model.agentSettingsLoaded = true;
    let requests = 0;
    model.agentFetch = async () => { requests++; return {}; };
    await model.saveAgentSettings(patch);
    assert.equal(requests, 0, JSON.stringify(patch));
    assert.equal(model.agentSettingsError, model.t('agentLimitsInvalid'));
  }
});

test('runtime limits accept both inclusive endpoints and independent valid patches', async () => {
  for (const patch of [
    { max_model_requests: 1, timeout_seconds: 10 }, { max_model_requests: 32, timeout_seconds: 600 },
    { max_model_requests: 12 }, { timeout_seconds: 45 }, { enabled: true },
  ]) {
    const { model } = harness();
    model.agentSettingsLoaded = true;
    let saved;
    model.agentFetch = async (url, options) => {
      if (options?.method === 'PATCH') saved = JSON.parse(options.body);
      return { enabled: false, max_model_requests: 8, timeout_seconds: 90, ...patch };
    };
    await model.saveAgentSettings(patch);
    assert.deepEqual(saved, patch);
    assert.equal(model.agentSettingsError, '');
  }
});

test('HTML inputs expose the same backend limits', () => {
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  for (const [id, min, max] of [['agent-request-limit', 1, 32], ['agent-timeout', 10, 600]]) {
    const input = html.match(new RegExp('<input id="' + id + '"[^>]*>'))[0];
    assert.match(input, new RegExp('min="' + min + '"'));
    assert.match(input, new RegExp('max="' + max + '"'));
  }
});

function timerRun(name = 'timer_create', result = { is_error: false }) {
  return { state: 'completed', events: [
    event(1, 'tool_call', { tool_name: 'mcp__shikigami__' + name, call_id: 'timer-call', arguments: '{"seconds":1200}' }),
    ...(result ? [event(2, 'tool_result', { call_id: 'timer-call', ...result })] : []),
  ] };
}

test('timer summaries require a completed run and its matching successful result', () => {
  const { model } = harness();
  model.locale = 'zh';
  assert.equal(model.agentRunLabel(timerRun()), '已创建20分钟计时器');
  assert.equal(model.agentRunLabel(timerRun('timer_list')), '已查询计时器');
  assert.equal(model.agentRunLabel(timerRun('timer_cancel')), '已取消计时器');
  for (const run of [timerRun('timer_create', null), timerRun('timer_create', { is_error: true }),
    timerRun('timer_create', { is_error: false, call_id: 'unrelated' }), timerRun('timer_create', {}),
    { ...timerRun(), state: 'running' }, { ...timerRun(), state: 'failed' }]) {
    assert.equal(model.agentRunLabel(run), model.agentStateLabel(run));
  }
  assert.equal(model.agentRunLabel({ ...timerRun(), state: 'cancelled' }), '任务已停止');
  model.locale = 'en';
  assert.equal(model.agentRunLabel(timerRun()), 'Created a 20-minute timer');
  assert.equal(model.agentRunLabel({ ...timerRun(), state: 'cancelled' }), 'Task stopped');
});

test('known tool names are friendly in both locales and unknown names remain exact', () => {
  const { model } = harness();
  for (const [locale, labels] of [['zh', ['创建计时器', '查询计时器', '取消计时器']], ['en', ['Create timer', 'List timers', 'Cancel timer']]]) {
    model.locale = locale;
    for (const [index, name] of ['timer_create', 'timer_list', 'timer_cancel'].entries()) {
      assert.equal(model.agentToolLabel('mcp__shikigami__' + name), labels[index]);
      assert.match(model.agentEventLabel(timerRun(name).events[0]), new RegExp(labels[index]));
    }
    assert.equal(model.agentToolLabel('other_tool'), 'other_tool');
  }
});

test('task timestamps normalize Unix seconds and ISO through the existing formatter', () => {
  const { model } = harness();
  const seconds = 1793347200;
  const expected = model.formatMsgTime(seconds);
  for (const value of [seconds, String(seconds), new Date(seconds * 1000).toISOString()]) {
    assert.equal(model.agentRunTime(value), expected);
  }
  assert.equal(model.agentRunTime('invalid'), '');
  assert.equal(model.agentRunTime(null), '');
});

test('real Vue proxy accepts the same request nonce despite different object identities', () => {
  const { model } = harness(undefined, { reactive: true });
  const rawContext = { sessionId: 'p1', viewVersion: 0, runKey: null, requestId: 'proxy-request-1' };
  model.agentChatContext = rawContext;
  assert.equal(realVue.isReactive(model.agentChatContext), true);
  assert.notEqual(rawContext, model.agentChatContext);
  assert.equal(realVue.toRaw(model.agentChatContext), rawContext);
  assert.equal(model.agentChatIsVisible(rawContext), true);
  assert.equal(model.agentChatIsVisible({ ...rawContext }), true);
  assert.equal(model.agentChatIsVisible({ ...rawContext, requestId: 'proxy-request-2' }), false);
  assert.equal(model.agentChatIsVisible({ ...rawContext, requestId: undefined }), false);
  assert.equal(model.agentChatIsVisible({ ...rawContext, sessionId: 'p2' }), false);
  assert.equal(model.agentChatIsVisible({ ...rawContext, viewVersion: 1 }), false);
  model.currentGroupId = 'group-1';
  assert.equal(model.agentChatIsVisible(rawContext), false);
  model.currentGroupId = null;
  model.agentChatContext = { ...rawContext, requestId: 123 };
  assert.equal(model.agentChatIsVisible({ ...rawContext, requestId: 123 }), false);
});

test('real Vue reactive send reaches chat with explicit origin session and clears spinner', async () => {
  const sent = [];
  const { model } = harness(async (url, options) => {
    sent.push({ url, body: JSON.parse(options.body), aborted: options.signal.aborted });
    return streamResponse([
      { agent_event: event(1, 'started') }, { token: 'Reactive reply' },
      { agent_event: event(2, 'completed') }, { done: true, message_id: 42 },
    ]);
  }, { reactive: true });
  await model.sendMessage();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, 'http://fixture.test/chat');
  assert.equal(sent[0].body.session_id, 'p1');
  assert.equal(sent[0].body.request_id, model.agentChatContext.requestId);
  assert.equal(sent[0].aborted, false);
  assert.equal(realVue.isReactive(model.agentChatContext), true);
  assert.equal(model.messages[1].content, 'Reactive reply');
  assert.equal(model.messages[1].id, 42);
  assert.equal(model.isStreaming, false);
  assert.equal(model.streamingText, '');
  assert.equal(model._chatAbortController, null);
  assert.equal(model.agentRuns[model.agentChatContext.runKey].state, 'completed');
  assert.equal(model.currentAgentRuns.length, 0);
  assert.deepEqual(Array.from(model.spoken), ['Reactive reply']);
});

function connectMessages(model, sandbox, session = 'p1') {
  sandbox.EventSource = class {
    constructor() { this.readyState = 1; }
    get [Symbol.toStringTag]() { return 'EventSource'; }
    close() { this.readyState = 2; }
  };
  model.subscribeEvents(session);
  return data => model._sseSource.onmessage({ data: JSON.stringify({ type: 'new_message', ...data }) });
}

test('cancelled done preserves the streamed prefix as one silent partial reply', async () => {
  const { model } = harness(async () => streamResponse([
    { agent_event: event(1, 'started') }, { token: 'Already visible' },
    { agent_event: event(2, 'cancelled') }, { done: true, message_id: 50, partial: true },
  ]), { reactive: true });
  await model.sendMessage();
  const replies = model.messages.filter(m => m.role === 'assistant');
  assert.equal(replies.length, 1);
  assert.equal(replies[0].content, 'Already visible');
  assert.equal(replies[0].partial, true);
  assert.equal(replies[0].request_id, model.agentChatContext.requestId);
  assert.equal(model.spoken.length, 0);
});

test('native abort preserves a prefix and server broadcast reconciles ID/content without duplicates or TTS', async () => {
  let model, sandbox, chatBody, abortRead, waiting;
  ({ model, sandbox } = harness(async (url, options) => {
    chatBody = JSON.parse(options.body);
    let first = true;
    return { ok: true, body: { getReader: () => ({ read: async () => {
      if (first) { first = false; return { done: false, value: new TextEncoder().encode([
        { agent_event: event(1, 'started') }, { token: 'Prefix' },
      ].map(f => 'data: ' + JSON.stringify(f) + '\n\n').join('')) }; }
      waiting = true;
      return new Promise((resolve, reject) => { abortRead = () => reject(new DOMException('Stopped', 'AbortError')); options.signal.addEventListener('abort', abortRead); });
    } }) } };
  }, { reactive: true }));
  const pending = model.sendMessage();
  while (!waiting) await new Promise(resolve => setImmediate(resolve));
  model.agentFetch = async () => ({ accepted: true, state: 'cancelling' });
  await model.abortChat();
  await pending;
  assert.equal(model.messages[1].content, 'Prefix');
  assert.equal(model.messages[1].partial, true);
  const broadcast = connectMessages(model, sandbox);
  const saved = { client_id: chatBody.client_id, role: 'assistant', content: 'Prefix saved', request_id: chatBody.request_id, message_id: 51, partial: true };
  broadcast(saved); broadcast(saved);
  assert.equal(model.messages.filter(m => m.role === 'assistant').length, 1);
  assert.equal(model.messages[1].id, 51);
  assert.equal(model.messages[1].content, 'Prefix saved');
  assert.equal(model.spoken.length, 0);
});

test('own broadcast after switching away and back appends once; stale fetch cannot overwrite it', async () => {
  let model, sandbox, deliver, waiting;
  ({ model, sandbox } = harness(async (url, options) => {
    const body = JSON.parse(options.body);
    let first = true;
    return { ok: true, body: { getReader: () => ({ read: async () => {
      if (first) {
        first = false; waiting = true;
        return new Promise(resolve => { deliver = () => {
          const broadcast = connectMessages(model, sandbox);
          const saved = { client_id: body.client_id, request_id: body.request_id, message_id: 52, role: 'assistant', content: 'Saved late reply', partial: true };
          broadcast(saved); broadcast(saved);
          resolve({ done: false, value: new TextEncoder().encode('data: {"token":"stale text"}\n\ndata: {"done":true}\n\n') });
        }; });
      }
      return { done: true };
    } }) } };
  }, { reactive: true }));
  const pending = model.sendMessage();
  while (!waiting) await new Promise(resolve => setImmediate(resolve));
  model.currentSessionId = 'p2'; model.agentViewChanged(); model.messages = [];
  model.currentSessionId = 'p1'; model.agentViewChanged(); model.messages = [];
  deliver(); await pending;
  assert.equal(model.messages.length, 1);
  assert.equal(model.messages[0].content, 'Saved late reply');
  assert.equal(model.messages[0].id, 52);
  assert.equal(model.messages[0].partial, true);
  assert.equal(model.spoken.length, 0);
});

test('visible own stream caches authoritative broadcast then done renders one reply', async () => {
  let model, sandbox;
  ({ model, sandbox } = harness(async (url, options) => {
    const body = JSON.parse(options.body);
    const broadcast = connectMessages(model, sandbox);
    broadcast({ client_id: body.client_id, request_id: body.request_id, message_id: 53, role: 'assistant', content: 'Complete reply' });
    assert.equal(model.messages.filter(m => m.role === 'assistant').length, 0);
    return streamResponse([{ token: 'Complete reply' }, { done: true, message_id: 53 }]);
  }, { reactive: true }));
  await model.sendMessage();
  assert.equal(model.messages.filter(m => m.role === 'assistant').length, 1);
  assert.equal(model.messages[1].request_id, model.agentChatContext.requestId);
  assert.deepEqual(Array.from(model.spoken), ['Complete reply']);
});

test('timers show current owner plus legacy unscoped entries, never other personas', async () => {
  const { model, app } = harness(async () => ({ ok: true, json: async () => ({ timers: [
    { id: 'own', session_id: 'p1', remaining: 60 }, { id: 'other', session_id: 'p2', remaining: 60 },
    { id: 'legacy', session_id: '', remaining: 60 }, { id: 'missing', remaining: 60 },
  ] }) }));
  await app.methods.refreshTimers.call(model);
  assert.deepEqual(Array.from(model.timerList, timer => timer.id), ['own', 'legacy', 'missing']);
});

test('view switches clear/refetch timers and discard a late earlier fetch', async () => {
  const requests = [];
  const { model, app } = harness(() => new Promise(resolve => requests.push(resolve)));
  model.refreshTimers = app.methods.refreshTimers.bind(model);
  model.timerList = [{ id: 'old', session_id: 'p1' }];
  const old = model.refreshTimers();
  model.currentSessionId = 'p2'; model.agentViewChanged();
  assert.equal(model.timerList.length, 0);
  assert.equal(requests.length, 2);
  requests[1]({ ok: true, json: async () => ({ timers: [{ id: 'new', session_id: 'p2', remaining: 30 }] }) });
  await new Promise(resolve => setImmediate(resolve));
  requests[0]({ ok: true, json: async () => ({ timers: [{ id: 'old', session_id: 'p1', remaining: 60 }] }) });
  await old;
  assert.deepEqual(Array.from(model.timerList, timer => timer.id), ['new']);
});

test('saved partial broadcast before done preserves authoritative content and stays silent', async () => {
  let model, sandbox;
  ({ model, sandbox } = harness(async (url, options) => {
    const body = JSON.parse(options.body);
    connectMessages(model, sandbox)({ client_id: body.client_id, request_id: body.request_id,
      role: 'assistant', content: 'Longer saved prefix', partial: true, message_id: 54 });
    return streamResponse([{ token: 'Longer' }, { done: true, message_id: 54, partial: true }]);
  }, { reactive: true }));
  await model.sendMessage();
  assert.equal(model.messages.filter(m => m.role === 'assistant').length, 1);
  assert.equal(model.messages[1].content, 'Longer saved prefix');
  assert.equal(model.messages[1].partial, true);
  assert.equal(model.spoken.length, 0);
});

test('history snapshots retain in-flight replies, preserve partial metadata, and deduplicate saved IDs', async () => {
  let resolveHistory;
  const { model } = harness(() => new Promise(resolve => { resolveHistory = resolve; }), { reactive: true });
  const pending = model.loadHistory('p1');
  model.reconcileChatMessage({ role: 'assistant', content: 'Saved partial', id: 55, partial: true,
    request_id: 'history-request', session_id: 'p1' }, true);
  resolveHistory({ ok: true, json: async () => ({ messages: [], total: 0 }) });
  await pending;
  assert.equal(model.messages.length, 1);
  const next = model.loadHistory('p1');
  resolveHistory({ ok: true, json: async () => ({ messages: [{ id: 55, role: 'assistant', content: 'Saved partial', partial: true }], total: 1 }) });
  await next;
  assert.equal(model.messages.length, 1);
  assert.equal(model.messages[0].request_id, 'history-request');
  assert.equal(model.messages[0].partial, true);
  const stale = model.loadHistory('p1');
  model.currentSessionId = 'p2'; model.agentViewChanged(); model.messages = [];
  resolveHistory({ ok: true, json: async () => ({ messages: [{ id: 55, role: 'assistant', content: 'Old profile' }] }) });
  await stale;
  assert.equal(model.messages.length, 0);
});

test('group history checks group ownership rather than a single-chat session variable', async () => {
  const { model } = harness(async () => ({ ok: true, json: async () => ({ messages: [{ id: 56, role: 'assistant', content: 'Group reply' }] }) }));
  model.currentSessionId = null; model.currentGroupId = 'g1';
  await model.loadGroupHistory('g1');
  assert.equal(model.messages[0].content, 'Group reply');
  model.messages = [];
  await model.loadGroupHistory('other-group');
  assert.equal(model.messages.length, 0);
});

test('partial server-originated broadcasts are never spoken and wrong-session SSE is ignored', () => {
  const { model, sandbox } = harness(undefined, { reactive: true });
  const broadcast = connectMessages(model, sandbox);
  broadcast({ client_id: '', request_id: 'server-partial', role: 'assistant', content: 'Partial text', partial: true, message_id: 57 });
  assert.equal(model.messages.length, 1);
  assert.equal(model.spoken.length, 0);
  model.currentSessionId = 'p2'; model.agentViewChanged(); model.messages = [];
  broadcast({ client_id: '', request_id: 'old-profile', role: 'assistant', content: 'Wrong chat', message_id: 58 });
  assert.equal(model.messages.length, 0);
});

test('partial replies remain deletable but do not enter manual TTS playback', async () => {
  let deleted;
  const { model } = harness(async (url, options) => { deleted = { url, method: options.method }; return { ok: true }; });
  model.messages = [{ id: 59, role: 'assistant', content: 'Partial', partial: true }];
  model._absoluteIdx = index => index;
  model.replayTTS(0);
  assert.equal(model.spoken.length, 0);
  await model.deleteMessage(0);
  assert.equal(deleted.method, 'DELETE');
  assert.match(deleted.url, /\/sessions\/p1\/messages\/by-id\/59$/);
  assert.equal(model.messages.length, 0);
});

test('run refresh prunes missing unchanged records only for the polled profile', async () => {
  const { model, sandbox } = harness(undefined, { reactive: true });
  model.receiveAgentEvent(event(1, 'completed', { run_id: 'removed' }));
  model.receiveAgentEvent(event(1, 'completed', { run_id: 'kept' }));
  model.receiveAgentEvent(event(1, 'completed', { run_id: 'other', profile_id: 'p2', session_id: 'p2' }));
  const polled = [];
  model.agentFetch = async url => {
    polled.push(url);
    return { runs: [{ ...event(1, 'completed', { run_id: 'kept' }), id: 'kept', state: 'completed', events: [] }] };
  };
  await sandbox.AgentUIMixin.methods.refreshAgentRuns.call(model);
  assert.equal(polled.length, 1);
  assert.match(polled[0], /profile_id=p1$/);
  assert.deepEqual(Object.values(model.agentRuns).map(run => run.run_id).sort(), ['kept', 'other']);
});

test('run refresh preserves missing entries changed or created during the fetch', async () => {
  const { model, sandbox } = harness(undefined, { reactive: true });
  model.receiveAgentEvent(event(1, 'started', { run_id: 'updated' }));
  model.receiveAgentEvent(event(1, 'completed', { run_id: 'removed' }));
  model.agentFetch = async () => {
    model.receiveAgentEvent(event(2, 'tool_call', { run_id: 'updated', call_id: 'c1', tool_name: 'timer_create' }));
    model.receiveAgentEvent(event(1, 'started', { run_id: 'new' }));
    return { runs: [] };
  };
  await sandbox.AgentUIMixin.methods.refreshAgentRuns.call(model);
  assert.deepEqual(Object.values(model.agentRuns).map(run => run.run_id).sort(), ['new', 'updated']);
  assert.equal(model.agentRuns[api.keyOf(event(1, 'started', { run_id: 'updated' }))].sequence, 2);
});

test('failed run refresh does not prune previously known records', async () => {
  const { model, sandbox } = harness(undefined, { reactive: true });
  model.receiveAgentEvent(event(1, 'completed'));
  model.agentFetch = async () => { throw new Error('offline'); };
  await sandbox.AgentUIMixin.methods.refreshAgentRuns.call(model);
  assert.equal(Object.values(model.agentRuns).length, 1);
  assert.equal(model.agentRunErrors.p1, 'offline');
});

test('pruning during cancel or approval leaves late acknowledgments harmless', async () => {
  for (const action of ['cancel', 'approval']) {
    const { model, sandbox } = harness(undefined, { reactive: true });
    model.receiveAgentEvent(event(1, action === 'approval' ? 'approval_requested' : 'started', { approval_id: 'a1' }));
    const key = api.keyOf(event(1));
    model.agentFetch = async url => {
      if (url.includes('?profile_id=')) return { runs: [] };
      await sandbox.AgentUIMixin.methods.refreshAgentRuns.call(model);
      return { accepted: true, state: 'cancelled' };
    };
    if (action === 'cancel') await model.cancelAgentRun(model.agentRuns[key]);
    else await model.answerAgentApproval(model.agentRuns[key], 'rejected');
    assert.equal(Object.values(model.agentRuns).length, 0);
  }
});

test('plain running task stays hidden while attached but is stoppable after A-B-A', async () => {
  const { model } = harness(undefined, { reactive: true });
  const context = { requestId: 'slow-a-request', sessionId: 'p1', viewVersion: 0, runKey: null };
  model.agentChatContext = context;
  model.isStreaming = true;
  model.receiveAgentEvent(event(1, 'started', { request_id: context.requestId }), context);
  assert.equal(model.currentAgentRuns.length, 0);
  assert.notEqual(context, model.agentChatContext);
  model.currentSessionId = 'p2'; model.agentViewChanged();
  assert.equal(model.currentAgentRuns.length, 0);
  model.currentSessionId = 'p1'; model.agentViewChanged();
  assert.equal(model.isStreaming, false);
  assert.equal(model.currentAgentRuns.length, 1);
  assert.equal(model.currentAgentRuns[0].state, 'running');
  assert.equal(model.agentWorkspaceOpen, false);
  let posted;
  model.agentFetch = async (url, options) => { posted = { url, body: JSON.parse(options.body) }; return { accepted: true, state: 'cancelling' }; };
  await model.cancelAgentRun(model.currentAgentRuns[0]);
  assert.match(posted.url, /\/r1\/cancel$/);
  assert.equal(posted.body.profile_id, 'p1');
  assert.equal(model.currentAgentRuns[0].cancelling, true);
  model.receiveAgentEvent(event(2, 'cancelled', { request_id: context.requestId }));
  assert.equal(model.currentAgentRuns.length, 0);
});

test('same visible view with a different request nonce cannot hide an older active run', () => {
  const { model } = harness(undefined, { reactive: true });
  model.receiveAgentEvent(event(1, 'started', { request_id: 'original-request' }));
  model.agentChatContext = { requestId: 'different-request', sessionId: 'p1', viewVersion: 0, runKey: api.keyOf(event(1)) };
  model.isStreaming = true;
  assert.equal(model.currentAgentRuns.length, 1);
  model.agentChatContext.requestId = 'original-request';
  assert.equal(model.currentAgentRuns.length, 0);
  model.agentChatContext.requestId = 'different-request';
  model.receiveAgentEvent(event(2, 'completed', { request_id: 'original-request' }));
  assert.equal(model.currentAgentRuns.length, 0);
  assert.equal(Object.values(model.agentRuns).length, 1);
});

test('actual switchSession detaches a known native single-chat run and switches API/history/view', async () => {
  const requests = [];
  const { model, sandbox } = harness(async (url, options) => {
    requests.push({ url, method: options?.method, body: options?.body ? JSON.parse(options.body) : undefined });
    if (url.endsWith('/api/agent/view')) return { ok: true, json: async () => ({ ok: true }) };
    if (url.endsWith('/sessions/switch')) return { ok: true, json: async () => ({ ok: true, display_name: 'Two' }) };
    if (url.includes('/sessions/p2/history')) return { ok: true, json: async () => ({ messages: [{ id: 60, role: 'assistant', content: 'Profile B history' }] }) };
    throw new Error('Unexpected request: ' + url);
  }, { reactive: true });
  model.sessions = [{ id: 'p1', display_name: 'One', is_current: true }, { id: 'p2', display_name: 'Two', avatar: 'two.png' }];
  model.isStreaming = true;
  model.streamingText = 'Profile A prefix';
  model.messages = [{ role: 'user', content: 'Profile A message' }];
  const context = { sessionId: 'p1', requestId: 'switch-native-request', viewVersion: 0, runKey: null };
  model.agentChatContext = context;
  model.receiveAgentEvent(event(1, 'started', { request_id: context.requestId }), context);
  let aborted = false, subscribed;
  model._chatAbortController = { abort() { aborted = true; } };
  model.subscribeEvents = session => { subscribed = session; };
  model.fetchStatus = model.fetchAseStatus = () => {};
  const stopWatching = realVue.watch(() => model.currentSessionId,
    () => sandbox.AgentUIMixin.watch.currentSessionId.call(model));
  try {
    await model.switchSession('p2');
    await realVue.nextTick();
    assert.equal(requests[0].url, 'http://fixture.test/sessions/switch');
    assert.equal(requests[0].method, 'POST');
    assert.deepEqual(requests[0].body, { session_id: 'p2' });
    assert.equal(requests.length, 3);
    assert.deepEqual(requests.find(request => request.url.endsWith('/api/agent/view')),
      { url: 'http://fixture.test/api/agent/view', method: 'POST', body: { group_chat: false } });
    assert.equal(model.currentSessionId, 'p2');
    assert.equal(model.currentSessionName, 'Two');
    assert.equal(model.currentSessionAvatar, 'two.png');
    assert.equal(subscribed, 'p2');
    assert.equal(model.sessions.find(s => s.id === 'p2').is_current, true);
    assert.equal(model.sessions.find(s => s.id === 'p1').is_current, false);
    assert.equal(model.messages.length, 1);
    assert.equal(model.messages[0].content, 'Profile B history');
    assert.equal(model.isStreaming, false);
    assert.equal(model.streamingText, '');
    assert.equal(model.agentChatIsVisible(context), false);
    assert.equal(model.agentRuns[context.runKey].profile_id, 'p1');
    assert.equal(model.agentRuns[context.runKey].state, 'running');
    assert.equal(model.currentAgentRuns.length, 0);
    assert.equal(aborted, false);
  } finally { stopWatching(); }
});

test('actual switchSession still blocks group, pre-run VLM, and legacy streams', async () => {
  for (const scenario of [
    { name: 'group', currentGroupId: 'g1', agentChatContext: { runKey: 'known', requestId: 'group-request' } },
    { name: 'pre-run VLM', pendingScreenshot: 'data:image/png;base64,AA==', agentChatContext: { runKey: null, requestId: 'vlm-request' } },
    { name: 'legacy', agentChatContext: null },
    { name: 'unknown native run', agentChatContext: { requestId: 'unknown-request', runKey: null } },
  ]) {
    let requested = false;
    const { model } = harness(async () => { requested = true; return { ok: true, json: async () => ({ ok: true }) }; }, { reactive: true });
    Object.assign(model, scenario, { isStreaming: true, streamingText: 'Existing reply' });
    await model.switchSession('p2');
    assert.equal(requested, false, scenario.name);
    assert.equal(model.currentSessionId, 'p1', scenario.name);
    assert.equal(model.isStreaming, true, scenario.name);
    assert.equal(model.streamingText, 'Existing reply', scenario.name);
  }
});
