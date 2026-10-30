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
    assert.equal(requests.length, 2);
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
