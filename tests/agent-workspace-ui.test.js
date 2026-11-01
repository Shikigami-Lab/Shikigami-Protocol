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
const componentSandbox = vm.createContext({ window: {} });
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/components.js'), 'utf8'), componentSandbox);
const taskCard = componentSandbox.window.SK_COMPONENTS['agent-task-card'];
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

function metadataHarness() {
  const { model, sandbox } = harness();
  model.agentWorkspaceOpen = true;
  model.agentRunsKnown.p1 = true;
  const file = { path: 'notes/plan.md', description: 'Old purpose', category: 'note', pinned: false, previewable: false };
  model.agentWorkspaceFiles = [file];
  model.agentWorkspaceSelected = file;
  model.agentWorkspaceMetadata = { description: 'New purpose', category: 'work', pinned: true };
  return { model, sandbox, file };
}

test('metadata PATCH uses same-origin fetch and updates selected file and inventory', async () => {
  const { model, sandbox } = metadataHarness();
  const calls = [];
  sandbox.fetch = async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => options ? ({ ok: true, file: { path: 'notes/plan.md', description: 'New purpose', category: 'work', pinned: true } }) : ({ runs: [] }) };
  };
  await model.saveAgentWorkspaceMetadata();
  assert.equal(calls[1].url, 'http://fixture.test/api/agent/workspace/metadata');
  assert.equal(calls[1].options.method, 'PATCH');
  assert.deepEqual(JSON.parse(calls[1].options.body), { profile_id: 'p1', path: 'notes/plan.md', description: 'New purpose', category: 'work', pinned: true });
  assert.equal(model.agentWorkspaceFiles[0].description, 'New purpose');
  assert.equal(model.agentWorkspaceSelected.pinned, true);
  assert.equal(model.agentWorkspaceMutationBusy, false);
});

for (const change of ['file', 'file-return', 'profile', 'close', 'group']) {
  for (const fail of [false, true]) test(`late metadata ${fail ? 'failure' : 'success'} cannot affect ${change} view`, async () => {
    const { model } = metadataHarness();
    const pending = deferred(); let sent = 0;
    model.agentFetch = async (url, options) => options ? (sent++, pending.promise) : ({ runs: [] });
    const saving = model.saveAgentWorkspaceMetadata();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(sent, 1);
    if (change.startsWith('file')) {
      model.agentFetch = async () => ({ versions: [] });
      await model.selectAgentWorkspaceFile({ path: 'other.txt', previewable: false });
      if (change === 'file-return') await model.selectAgentWorkspaceFile({ path: 'notes/plan.md', previewable: false });
    } else if (change === 'profile') { model.currentSessionId = 'p2'; model.agentViewChanged(); }
    else if (change === 'close') { model.closeAgentWorkspace(); model.agentWorkspaceOpen = true; }
    else model.currentGroupId = 'group';
    model.agentWorkspaceMetadata = { description: 'Current draft', category: 'material', pinned: false };
    model.agentWorkspaceError = 'Current error';
    if (fail) pending.reject(new Error('Old error'));
    else pending.resolve({ ok: true, file: { path: 'notes/plan.md', description: 'Stale', category: 'work', pinned: true } });
    await saving;
    assert.equal(model.agentWorkspaceMetadata.description, 'Current draft');
    assert.equal(model.agentWorkspaceError, 'Current error');
    assert.equal(model.agentWorkspaceMutationBusy, false);
  });
}

test('metadata validation and workspace lock prevent writes; preflight rechecks task status', async () => {
  const { model } = metadataHarness(); let writes = 0;
  model.agentFetch = async (url, options) => { if (options) writes++; return { runs: [{ profile_id: 'p1', state: 'running' }] }; };
  for (const invalid of [{ description: 'x'.repeat(201) }, { category: 'invalid' }, { pinned: 'yes' }]) {
    model.agentWorkspaceMetadata = { description: '', category: 'note', pinned: false, ...invalid };
    await model.saveAgentWorkspaceMetadata();
  }
  assert.equal(writes, 0);
  model.agentWorkspaceMetadata = { description: 'Valid', category: 'other', pinned: false };
  await model.saveAgentWorkspaceMetadata();
  assert.equal(writes, 0);
  assert.match(model.agentWorkspaceError, /running/);
  model.agentWorkspaceMutationBusy = true;
  await model.saveAgentWorkspaceMetadata();
  assert.equal(writes, 0);
});

test('drawer keyboard wraps three tabs and supports Home/End while ignoring other keys', () => {
  const { model } = harness(); model.agentWorkspaceOpen = true;
  model.refreshTodos = () => {};
  const focused = [], prevented = [];
  const buttons = ['files', 'plans', 'tasks'].map(id => ({ focus() { focused.push(id); } }));
  const press = key => model.navigateAgentWorkspaceTabs({ key, preventDefault() { prevented.push(key); }, currentTarget: { querySelectorAll: () => buttons } });
  press('ArrowRight'); assert.equal(model.agentWorkspaceTab, 'plans');
  press('ArrowRight'); assert.equal(model.agentWorkspaceTab, 'tasks');
  press('ArrowRight'); assert.equal(model.agentWorkspaceTab, 'files');
  press('ArrowLeft'); assert.equal(model.agentWorkspaceTab, 'tasks');
  press('Home'); assert.equal(model.agentWorkspaceTab, 'files');
  press('End'); assert.equal(model.agentWorkspaceTab, 'tasks');
  press('Escape'); assert.equal(prevented.length, 6);
  assert.deepEqual(focused, ['plans', 'tasks', 'files', 'tasks', 'files', 'tasks']);
});

test('Files tab loads inventory after opening Plans first', async () => {
  const { model } = harness(); let lists = 0;
  model.refreshTodos = async () => {};
  model.refreshAgentWorkspace = async () => { lists++; };
  await model.openAgentWorkspace('plans');
  await model.selectAgentWorkspaceTab('files');
  assert.equal(lists, 1);
});

test('inventory refresh updates unchanged file metadata and preserves unsaved descriptions', async () => {
  const { model, file } = metadataHarness();
  model.agentWorkspaceMetadata = model.agentWorkspaceFileMetadata(file);
  model.agentFetch = async () => ({ files: [{ ...file, description: 'Updated externally', pinned: true }] });
  await model.refreshAgentWorkspace();
  assert.equal(model.agentWorkspaceMetadata.description, 'Updated externally');
  assert.equal(model.agentWorkspaceSelected.pinned, true);
  model.agentWorkspaceMetadata.description = 'Unsaved draft';
  await model.refreshAgentWorkspace();
  assert.equal(model.agentWorkspaceMetadata.description, 'Unsaved draft');
});

test('metadata preflight cannot submit for a file selected away from and back to', async () => {
  const { model } = metadataHarness(); const pending = deferred(); let writes = 0;
  model.agentFetch = async (url, options) => { if (options) writes++; return pending.promise; };
  const saving = model.saveAgentWorkspaceMetadata();
  model.agentWorkspaceFileSequence += 2;
  pending.resolve({ runs: [] }); await saving;
  assert.equal(writes, 0);
});

test('malformed metadata success cannot update inventory or selection', async () => {
  const { model } = metadataHarness();
  model.agentFetch = async (url, options) => options ? ({ ok: true, file: { path: 'another.txt', description: 'Wrong file', category: 'note', pinned: false } }) : ({ runs: [] });
  await model.saveAgentWorkspaceMetadata();
  assert.equal(model.agentWorkspaceSelected.description, 'Old purpose');
  assert.equal(model.agentWorkspaceFiles[0].description, 'Old purpose');
  assert.equal(model.agentWorkspaceError, model.t('agentWorkspaceInvalid'));
});

test('drawer retains focus wrap, Escape close and opener restoration with Plans selected', async () => {
  const { model, sandbox } = harness();
  model.agentWorkspaceOpen = true; model.agentDrawerNarrow = true; model.agentWorkspaceTab = 'plans';
  const focused = [], first = { tabIndex: 0, getClientRects: () => [1], focus: () => focused.push('close') };
  const last = { tabIndex: 0, getClientRects: () => [1], focus: () => focused.push('add') };
  const hidden = { tabIndex: 0, getClientRects: () => [] };
  model.$refs = { agentWorkspaceDrawer: { querySelectorAll(selector) {
    assert.match(selector, /input:not\(:disabled\)/, 'fieldset-disabled metadata inputs must stay out of focus wrap');
    return [first, last, hidden];
  }, contains: element => [first, last].includes(element) } };
  let prevented = 0;
  sandbox.document.activeElement = first;
  model.trapAgentWorkspaceFocus({ key: 'Tab', shiftKey: true, preventDefault() { prevented++; } });
  sandbox.document.activeElement = last;
  model.trapAgentWorkspaceFocus({ key: 'Tab', shiftKey: false, preventDefault() { prevented++; } });
  assert.deepEqual(focused, ['add', 'close']); assert.equal(prevented, 2);
  model._agentDrawerOpener = { isConnected: true, focus: () => focused.push('opener') };
  model.closeAgentWorkspace();
  assert.equal(model.agentWorkspaceOpen, false); assert.equal(focused.at(-1), 'opener');
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  assert.match(html, /@keydown\.esc\.stop\.prevent="closeAgentWorkspace"/);
  assert.match(html, /@keydown\.tab="trapAgentWorkspaceFocus"/);
  assert.match(html, /role="tablist"[^>]*@keydown="navigateAgentWorkspaceTabs"/);
  for (const tab of ['files', 'plans', 'tasks']) {
    assert.match(html, new RegExp('id="agent-' + tab + '-tab"[^>]*role="tab"[^>]*:aria-selected="agentWorkspaceTab === \'' + tab + '\''));
    assert.match(html, new RegExp('id="agent-' + tab + '-panel" role="tabpanel" aria-labelledby="agent-' + tab + '-tab"'));
  }
});
const event = (sequence, type = 'status', extra = {}) => ({
  run_id: 'r1', profile_id: 'p1', session_id: 'p1', sequence, type, status: 'running', ...extra,
});

test('task details follow their own assistant reply, with initiating user as pending fallback', () => {
  const { model } = harness();
  const user = { id: 11, role: 'user', session_id: 'p1', request_id: 'client-one' };
  const other = { id: 12, role: 'assistant', session_id: 'p1', agent_run_id: 'other' };
  model.messages = [user, other];
  const run = { ...event(2, 'tool_call'), state: 'running', user_message_id: 11,
    client_request_id: 'client-one', events: [{ type: 'tool_call' }] };
  model.agentRuns = { [api.keyOf(run)]: run };
  assert.equal(model.messageAgentRuns(user)[0].run_id, 'r1');
  assert.equal(model.messageAgentRuns(other).length, 0);
  const reply = { id: 13, role: 'assistant', session_id: 'p1', agent_run_id: 'r1' };
  model.messages.push(reply);
  assert.equal(model.messageAgentRuns(user).length, 0);
  assert.equal(model.messageAgentRuns(reply)[0].run_id, 'r1');
  model.currentSessionId = 'p2';
  assert.equal(model.messageAgentRuns(reply).length, 0);
});

test('an owning HTTP event binds pre-persistence task details, broadcast alone cannot guess ownership', () => {
  const { model } = harness();
  const user = { role: 'user', session_id: 'p1', request_id: 'client-one' };
  model.messages = [user];
  model.receiveAgentEvent(event(1, 'tool_call'), { sessionId: 'p1' });
  assert.equal(model.messageAgentRuns(user).length, 0);
  model.receiveAgentEvent(event(2, 'tool_call'), { sessionId: 'p1', requestId: 'client-one' });
  assert.equal(model.messageAgentRuns(user)[0].run_id, 'r1');
});

test('tasks with no visible message remain available in the drawer', () => {
  const { model } = harness();
  model.receiveAgentEvent(event(1, 'approval_requested', { approval_id: 'a1' }));
  assert.equal(model.currentAgentRuns.length, 1);
  assert.equal(model.agentActiveTaskCount, 1);
  model.refreshAgentWorkspace = () => {};
  model.selectAgentWorkspaceTab('tasks');
  assert.equal(model.agentWorkspaceOpen, true);
  assert.equal(model.agentWorkspaceTab, 'tasks');
  model.selectAgentWorkspaceTab('invalid');
  assert.equal(model.agentWorkspaceTab, 'tasks');
});

test('reactive message mapping prefers the bound final reply over an older partial', () => {
  const { model } = harness(undefined, { reactive: true });
  model.messages = [
    { id: 1, role: 'user', session_id: 'p1' },
    { id: 2, role: 'assistant', session_id: 'p1', agent_run_id: 'r1', partial: true },
    { id: 3, role: 'assistant', session_id: 'p1', agent_run_id: 'r1' },
  ];
  const run = { ...event(3, 'completed'), state: 'completed', user_message_id: 1,
    assistant_message_id: 3, events: [{ type: 'tool_call' }] };
  model.agentRuns = { [api.keyOf(run)]: run };
  assert.equal(model.messageAgentRuns(model.messages[1]).length, 0);
  assert.equal(model.messageAgentRuns(model.messages[2]).length, 1);
});

test('artifact opening cannot preview a previous profile after an awaited file listing', async () => {
  const { model } = harness();
  let resolve;
  model.refreshAgentWorkspace = () => new Promise(r => { resolve = r; });
  const selected = [];
  model.selectAgentWorkspaceFile = async file => selected.push(file.path);
  const opening = model.openAgentArtifact({ path: 'notes.md' });
  assert.equal(model.agentWorkspaceTab, 'files');
  model.currentSessionId = 'p2'; model.agentViewChanged();
  model.agentWorkspaceFiles = [{ path: 'notes.md' }];
  resolve(); await opening;
  assert.equal(selected.length, 0);
  for (const path of ['../secret', '/workspace/file', 'https://example.test', 'a\\b']) {
    assert.equal(model.agentArtifactPath(path), '');
  }
});

test('a later artifact request wins even when the first listing completes last', async () => {
  const { model } = harness();
  const pending = [], selected = [];
  model.refreshAgentWorkspace = () => new Promise(resolve => pending.push(resolve));
  model.selectAgentWorkspaceFile = async file => selected.push(file.path);
  const first = model.openAgentArtifact('a.txt');
  const second = model.openAgentArtifact('b.txt');
  model.agentWorkspaceFiles = [{ path: 'a.txt' }, { path: 'b.txt' }];
  pending[1](); await second;
  pending[0](); await first;
  assert.deepEqual(selected, ['b.txt']);
});

test('mobile drawer closes before opening timers or returning a background task to chat', () => {
  const { model } = harness();
  model.agentDrawerNarrow = true; model.agentWorkspaceOpen = true;
  model.toggleTimerPanel = () => { model.showTimerPanel = true; };
  model.openAgentTimers();
  assert.equal(model.agentWorkspaceOpen, false);
  assert.equal(model.showTimerPanel, true);
  const context = { sessionId: 'p1', requestId: 'client', viewVersion: 0 };
  model.agentChatContext = context; model.isStreaming = true;
  model.receiveAgentEvent(event(1, 'started'), context);
  model.agentWorkspaceOpen = true;
  assert.equal(model.agentCanDetach, true);
  model.detachAgentChat();
  assert.equal(model.agentWorkspaceOpen, false);
  assert.equal(model.isStreaming, false);
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  assert.match(html, /<button v-if="agentCanDetach"[^>]*class="toolbar-btn"[^>]*[\s\S]*?@click="detachAgentChat"/);
});

test('terminal host errors survive streamed events and later polling', async () => {
  const { model } = harness();
  const failure = event(2, 'failed', { error: 'Cleanup failed; workspace is blocked' });
  model.receiveAgentEvent(event(1, 'started'));
  model.receiveAgentEvent(failure);
  const key = api.keyOf(failure);
  assert.equal(model.agentRuns[key].error, failure.error);
  assert.equal(model.agentEventText(failure), failure.error);
  model.agentFetch = async () => ({ runs: [{ ...failure, id: 'r1', state: 'failed', events: [failure] }] });
  await model.refreshAgentRuns();
  assert.equal(model.agentRuns[key].error, failure.error);
});

function harness(fetchImpl = async () => ({ ok: true, json: async () => ({}) }), { reactive = false } = {}) {
  let app;
  const sandbox = { console, AbortController, DOMException, TextDecoder, Uint8Array, URL, Blob, FormData,
    crypto: require('node:crypto').webcrypto, fetch: fetchImpl,
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: fn => fn(),
    localStorage: { getItem: () => null, setItem() {} },
    location: { origin: 'http://fixture.test', protocol: 'http:', hostname: 'fixture.test' },
    document: { addEventListener() {}, createElement: () => ({ click() {}, remove() {} }), body: { appendChild() {} } }, marked: { setOptions() {} },
    Vue: { createApp(value) { app = value; return { component() {}, mount() {} }; } },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  for (const file of ['api-paths.js', 'i18n.js', 'agent-ui.js', 'settings.js', 'compose-assist.js', 'app.js']) {
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


test('continue selects a terminal task without sending and sends its ID only on the next manual reply', async () => {
  const sent = [];
  const { model } = harness(async (url, options) => { sent.push(JSON.parse(options.body)); return streamResponse([{ done: true }]); });
  const run = { id: 'r1', profile_id: 'p1', session_id: 'p1', state: 'cancelled', origin: 'user' };
  model.selectAgentContinuation(run);
  assert.equal(sent.length, 0);
  assert.equal(model.agentContinueRunId, 'r1');
  model.inputText = 'Please finish the report';
  await model.sendMessage();
  assert.equal(sent[0].continue_run_id, 'r1');
  assert.equal(sent[0].conversation_only, undefined);
  assert.equal(model.agentContinueRunId, '');
});

test('continuation is rejected for other profiles, running tasks, groups and cleared on view change', () => {
  const { model } = harness();
  for (const extra of [{ profile_id: 'p2' }, { state: 'running' }, { session_id: 'p2' }]) {
    model.selectAgentContinuation({ id: 'r1', profile_id: 'p1', session_id: 'p1', state: 'completed', ...extra });
    assert.equal(model.agentContinueRunId, '');
  }
  model.agentContinueRunId = 'r1';
  model.agentViewChanged();
  assert.equal(model.agentContinueRunId, '');
});

test('detach never aborts, background task forces a plain foreground reply and old tokens/finally stay isolated', async () => {
  const reads = [], requests = [];
  const { model } = harness(async (url, options) => {
    requests.push({ body: JSON.parse(options.body), signal: options.signal });
    return { ok: true, body: { getReader: () => ({ read: () => new Promise(resolve => reads.push(resolve)) }) } };
  }, { reactive: true });
  const tick = () => new Promise(resolve => setImmediate(resolve));
  const frame = values => ({ done: false, value: new TextEncoder().encode(values.map(v => 'data: ' + JSON.stringify(v) + '\n\n').join('')) });
  const old = model.sendMessage(); await tick();
  reads.shift()(frame([{ agent_event: event(1, 'started', { request_id: requests[0].body.request_id }) }, { token: 'Old prefix' }])); await tick();
  const oldContext = model.agentChatContext;
  model.detachAgentChat();
  assert.equal(oldContext.detached, true);
  assert.equal(requests[0].signal.aborted, false);
  assert.equal(model.isStreaming, false);
  model.inputText = 'new conversation';
  const next = model.sendMessage(); await tick();
  assert.equal(requests[1].body.conversation_only, true);
  assert.notEqual(requests[0].body.request_id, requests[1].body.request_id);
  const oldRead = reads.shift(), newRead = reads.shift();
  newRead(frame([{ token: 'New reply' }])); await tick();
  oldRead(frame([{ token: 'LATE OLD TOKEN' }, { agent_event: event(2, 'completed') }, { done: true }])); await tick();
  assert.equal(model.streamingText, 'New reply');
  assert.equal(model.isStreaming, true);
  // The foreground read was enqueued before the detached read.
  const finishNew = reads.shift(), finishOld = reads.shift();
  finishOld({ done: true }); await old;
  assert.equal(model.isStreaming, true);
  finishNew(frame([{ done: true }])); await tick();
  reads.shift()({ done: true }); await next;
  assert.equal(model.messages.filter(m => m.role === 'assistant' && !m.partial).at(-1).content, 'New reply');
});

test('history preserves cancelled/failed/completed state and task identity', async () => {
  const { model } = harness(async () => ({ ok: true, json: async () => ({ messages: [
    { id: 1, role: 'assistant', content: 'partial', generation_status: 'cancelled', agent_run_id: 'r1' },
    { id: 2, role: 'assistant', content: 'failed', generation_status: 'failed', agent_run_id: 'r2' },
    { id: 3, role: 'assistant', content: 'complete', generation_status: 'completed', agent_run_id: 'r3' },
  ] }) }));
  await model.loadHistory('p1');
  assert.deepEqual(model.messages.map(m => m.partial), [true, true, false]);
  assert.equal(model.messages[0].agent_run_id, 'r1');
  assert.equal(model.messages[1].generation_status, 'failed');
});

test('workspace URLs encode both profile and path', () => {
  const { sandbox } = harness();
  const url = new URL(sandbox.API_PATHS.agentWorkspaceFile('p&1', 'x/<img>#?.html', true), 'http://fixture.test');
  assert.equal(url.searchParams.get('profile_id'), 'p&1');
  assert.equal(url.searchParams.get('path'), 'x/<img>#?.html');
  assert.equal(url.searchParams.get('download'), 'true');
});

test('late workspace files cannot enter another profile or a reopened view', async () => {
  const { model } = harness();
  let resolve;
  model.agentFetch = () => new Promise(r => { resolve = r; });
  model.agentWorkspaceOpen = true;
  const pending = model.refreshAgentWorkspace();
  model.currentSessionId = 'p2'; model.agentViewChanged();
  resolve({ files: [{ path: 'private-p1.txt' }] }); await pending;
  assert.equal(model.agentWorkspaceFiles.length, 0);
});

test('upload/restore refuse a task in the owning profile but not another profile', async () => {
  const { model } = harness();
  model.agentWorkspaceOpen = true;
  model.agentWorkspaceSelected = { path: 'report.txt' };
  const calls = [];
  model.agentFetch = async (url, options) => { calls.push([url, options]); return { runs: [{ id: 'r1', profile_id: 'p1', session_id: 'p1', state: 'running' }] }; };
  await model.restoreAgentWorkspaceVersion({ id: 'v1' });
  assert.equal(calls.some(([, options]) => options?.method === 'POST'), false);
  assert.match(model.agentWorkspaceError, /running/i);
});

test('native labels and object results are readable text', () => {
  const { model } = harness();
  assert.equal(model.agentToolLabel('mcp__shikigami__read'), 'Read file');
  assert.equal(model.agentToolLabel('job_output'), 'Read job output');
  assert.equal(model.agentToolLabel('mcp__shikigami__job_kill'), 'Stop job');
  assert.equal(model.agentEventText({ type: 'tool_call', arguments: { path: '<img onerror=x>' } }), '{\n  "path": "<img onerror=x>"\n}');
});

test('settings omitted capability flags default to false', async () => {
  const { model } = harness();
  model.agentFetch = async () => ({ enabled: true, max_model_requests: 8, timeout_seconds: 60 });
  await model.loadAgentSettings();
  for (const key of ['workspace_enabled', 'code_enabled', 'web_enabled', 'history_enabled']) assert.equal(model.agentSettingsForm[key], false);
});

test('detached final response has its own bubble even without a broadcast and never overwrites foreground text', () => {
  const { model } = harness();
  model.agentChatContext = { requestId: 'new', sessionId: 'p1', viewVersion: 0 };
  model.streamingText = 'foreground';
  const old = { detached: true, detachedText: 'old ', requestId: 'old', sessionId: 'p1', viewVersion: 0 };
  model.receiveDetachedChatFrame(old, { token: 'answer' });
  model.receiveDetachedChatFrame(old, { done: true, message_id: 9, agent_run_id: 'r-old', generation_status: 'completed' });
  assert.equal(model.streamingText, 'foreground');
  assert.equal(model.messages[0].content, 'old answer');
  assert.equal(model.messages[0].request_id, 'old');
  assert.equal(model.messages[0].agent_run_id, 'r-old');
  assert.equal(model.spoken.length, 0);
  model.reconcileChatMessage({ id: 9, request_id: 'old', session_id: 'p1', role: 'assistant', content: 'server answer' }, true);
  model.receiveDetachedChatFrame(old, { done: true, message_id: 9 });
  assert.equal(model.messages.length, 1);
  assert.equal(model.messages[0].content, 'server answer');
});

test('preview renders HTML/SVG as text, uses blob URLs for raster images and revokes on close', async () => {
  const { model, sandbox } = harness();
  const urls = [], revoked = [];
  sandbox.URL = { createObjectURL: blob => { urls.push(blob); return 'blob:fixture'; }, revokeObjectURL: url => revoked.push(url) };
  model.agentWorkspaceOpen = true;
  model.agentFetch = async () => ({ versions: [{ id: 'v1', created_at: 10 }] });
  model.agentWorkspaceResponse = async () => ({ blob: async () => new Blob(['<script>bad()</script>'], { type: 'text/html' }) });
  await model.selectAgentWorkspaceFile({ path: 'x.html', size: 24, mime: 'text/html' });
  assert.equal(model.agentWorkspaceText, '<script>bad()</script>');
  assert.equal(model.agentWorkspaceImage, '');
  assert.equal(urls.length, 0);
  model.agentWorkspaceResponse = async () => ({ blob: async () => new Blob(['image'], { type: 'image/png' }) });
  await model.selectAgentWorkspaceFile({ path: 'x.png', size: 5 });
  assert.equal(model.agentWorkspaceImage, 'blob:fixture');
  model.closeAgentWorkspace();
  assert.deepEqual(revoked, ['blob:fixture']);
});

test('late preview and version responses cannot replace a newer selected file', async () => {
  const { model } = harness();
  model.agentWorkspaceOpen = true;
  let firstBlob, firstVersions;
  model.agentFetch = url => url.includes('first.txt') ? new Promise(r => { firstVersions = r; }) : Promise.resolve({ versions: [{ id: 'second-version' }] });
  model.agentWorkspaceResponse = async url => ({ blob: () => url.includes('first.txt') ? new Promise(r => { firstBlob = r; }) : Promise.resolve(new Blob(['second'], { type: 'text/plain' })) });
  const first = model.selectAgentWorkspaceFile({ path: 'first.txt' });
  await new Promise(resolve => setImmediate(resolve));
  await model.selectAgentWorkspaceFile({ path: 'second.txt' });
  firstBlob(new Blob(['first'], { type: 'text/plain' })); firstVersions({ versions: [{ id: 'first-version' }] });
  await first;
  assert.equal(model.agentWorkspaceText, 'second');
  assert.equal(model.agentWorkspaceVersions[0].id, 'second-version');
});

test('upload stays multipart and profile scoped; restore posts the selected version and refreshes preview', async () => {
  const calls = [];
  const { model } = harness(async (url, options) => { calls.push([url, options]); return { ok: true }; });
  model.agentWorkspaceOpen = true;
  model.agentFetch = async (url, options) => {
    calls.push([url, options]);
    if (url.includes('/runs')) return { runs: [{ id: 'other', profile_id: 'p2', state: 'running' }] };
    if (url.includes('/files')) return { files: [{ path: 'report.txt' }] };
    if (url.includes('/versions')) return { versions: [{ id: 'v1' }] };
    return { ok: true };
  };
  const input = { files: [new Blob(['upload'])], value: 'selected' };
  await model.uploadAgentWorkspaceFile({ target: input });
  const upload = calls.find(([url]) => url.endsWith('/upload'));
  assert.equal(upload[1].body.get('profile_id'), 'p1');
  assert.equal(await upload[1].body.get('file').text(), 'upload');
  assert.equal(upload[1].headers, undefined);
  assert.equal(input.value, '');
  model.agentWorkspaceSelected = { path: 'report.txt' };
  model.agentWorkspaceResponse = async () => ({ blob: async () => new Blob(['restored'], { type: 'text/plain' }) });
  await model.restoreAgentWorkspaceVersion({ id: 'v1' });
  const restore = calls.find(([url]) => url.endsWith('/restore'));
  assert.deepEqual(JSON.parse(restore[1].body), { profile_id: 'p1', path: 'report.txt', version_id: 'v1' });
  assert.equal(model.agentWorkspaceText, 'restored');
});

test('workspace mutation preflight failure and view change send no mutation', async () => {
  const { model } = harness();
  model.agentWorkspaceOpen = true; model.agentWorkspaceSelected = { path: 'file.txt' };
  let posts = 0;
  model.agentFetch = async (url, options) => { if (options) posts++; throw new Error('offline'); };
  await model.restoreAgentWorkspaceVersion({ id: 'v1' });
  assert.equal(posts, 0); assert.equal(model.agentWorkspaceError, 'offline');
  let resolve;
  model.agentFetch = () => new Promise(r => { resolve = r; });
  const pending = model.restoreAgentWorkspaceVersion({ id: 'v1' });
  model.closeAgentWorkspace(); model.agentWorkspaceOpen = true;
  resolve({ runs: [] }); await pending;
  assert.equal(posts, 0); assert.equal(model.agentWorkspaceMutationBusy, false);
});

test('profile overrides inherit and PATCH only the chosen capability', async () => {
  const requests = [];
  const { model } = harness(async (url, options) => { requests.push([url, options]); return { ok: true, json: async () => ({ ok: true }) }; });
  model.selectedProfileId = 'p1'; model.profiles = [{ profile_id: 'p1', tool_configs: { agent: { web_enabled: false } } }];
  assert.equal(model.getProfileAgentPermission('web_enabled'), false);
  assert.equal(model.getProfileAgentPermission('workspace_enabled'), null);
  model.loadProfiles = async () => {}; model.loadToolsConfig = async () => {};
  await model.saveToolConfig('agent', { web_enabled: true }, 'p1');
  assert.equal(requests[0][0], 'http://fixture.test/api/profiles/p1/tool_configs');
  assert.deepEqual(JSON.parse(requests[0][1].body), { tool_id: 'agent', config: { web_enabled: true } });
});

test('terminal snapshot metadata survives event reduction and enables continuation from history', async () => {
  const { model, sandbox } = harness();
  model.agentFetch = async () => ({ runs: [{ id: 'r1', profile_id: 'p1', session_id: 'p1', state: 'completed', origin: 'user',
    user_message_id: 1, assistant_message_id: 2, parent_run_id: 'r0', artifacts: [{ path: 'report.txt' }],
    events: [event(1, 'started'), event(2, 'completed')] }] });
  await sandbox.AgentUIMixin.methods.refreshAgentRuns.call(model);
  const run = Object.values(model.agentRuns)[0];
  assert.equal(run.assistant_message_id, 2);
  assert.equal(run.parent_run_id, 'r0');
  assert.equal(run.artifacts[0].path, 'report.txt');
  model.continueAgentMessage({ agent_run_id: 'r1', generation_status: 'completed', session_id: 'p1' });
  assert.equal(model.agentContinueRunId, 'r1');
});

test('workspace template uses escaped text and blob image state, with no iframe or raw HTML', () => {
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  const section = html.match(/<aside\b[^>]*id="agent-workspace-drawer"[\s\S]*?<\/aside>/)[0];
  assert.ok(section.includes('{{ agentWorkspaceText }}'));
  assert.ok(section.includes(':src="agentWorkspaceImage"'));
  assert.doesNotMatch(section, /v-html|<iframe|<object|<embed/);
  assert.equal(typeof realVue.compile(section, { decodeEntities: value => value }), 'function');
});

test('late history from an earlier visit cannot replace the returned-to chat', async () => {
  let resolve;
  const { model } = harness(async () => new Promise(r => { resolve = r; }));
  const pending = model.loadHistory('p1');
  model.currentSessionId = 'p2'; model.agentViewChanged();
  model.currentSessionId = 'p1'; model.agentViewChanged();
  model.messages = [{ id: 2, role: 'assistant', content: 'current' }];
  resolve({ ok: true, json: async () => ({ messages: [{ id: 1, role: 'assistant', content: 'stale' }] }) });
  await pending;
  assert.equal(model.messages[0].content, 'current');
});

test('history requested before a terminal broadcast cannot downgrade its final content/status', async () => {
  let resolve;
  const { model } = harness(async () => new Promise(r => { resolve = r; }));
  model.messages = [{ id: 1, request_id: 'r1', session_id: 'p1', role: 'assistant', content: 'prefix', partial: true }];
  const pending = model.loadHistory('p1');
  model.reconcileChatMessage({ id: 1, request_id: 'r1', session_id: 'p1', role: 'assistant', content: 'final',
    generation_status: 'completed', agent_run_id: 'task1', partial: false }, true);
  resolve({ ok: true, json: async () => ({ messages: [{ id: 1, request_id: 'r1', role: 'assistant', content: 'prefix', partial: true, generation_status: 'cancelled' }] }) });
  await pending;
  assert.equal(model.messages[0].content, 'final');
  assert.equal(model.messages[0].partial, false);
  assert.equal(model.messages[0].generation_status, 'completed');
});

test('stopping a background task never aborts the new foreground request', async () => {
  const { model } = harness();
  model.receiveAgentEvent(event(1, 'started', { request_id: 'old' }));
  const run = Object.values(model.agentRuns)[0];
  let foregroundAborts = 0;
  model.isStreaming = true;
  model.agentChatContext = { requestId: 'new', sessionId: 'p1', viewVersion: 0, runKey: null };
  model._chatAbortController = { abort() { foregroundAborts++; } };
  model.agentFetch = async () => ({ accepted: true, state: 'cancelling' });
  await model.cancelAgentRun(run);
  assert.equal(foregroundAborts, 0);
  await model.abortChat();
  assert.equal(foregroundAborts, 1);
});

test('profile capability save does not reload old tools into a newly selected profile', async () => {
  const { model } = harness(async () => ({ ok: true, json: async () => ({ ok: true }) }));
  model.selectedProfileId = 'p1';
  const loaded = [];
  model.loadProfiles = async () => { model.selectedProfileId = 'p2'; };
  model.loadToolsConfig = async pid => loaded.push(pid);
  await model.saveToolConfig('agent', { web_enabled: false }, 'p1');
  assert.deepEqual(loaded, []);
});

test('HTTP stream binding attaches native run IDs without equating them to the client nonce', () => {
  const { model } = harness(undefined, { reactive: true });
  const context = { requestId: 'client-nonce', sessionId: 'p1', viewVersion: 0 };
  model.agentChatContext = context; model.isStreaming = true;
  model.receiveAgentEvent(event(1, 'started', { request_id: 'r1' }), context);
  assert.equal(model.agentCanDetach, true);
  model.agentChatContext.requestId = 'another-client-request';
  assert.equal(model.agentCanDetach, false);
});

test('workspace styles use defined theme tokens', () => {
  const css = fs.readFileSync(path.join(__dirname, '../static/css/styles.css'), 'utf8');
  const added = css.slice(css.indexOf('/* Workspace shares'), css.indexOf(':root {'));
  for (const [, token] of added.matchAll(/var\((--[\w-]+)\)/g)) assert.ok(css.includes(token + ':'), token);
});

test('authoritative reply content without optional metadata retains its known task identity', () => {
  const { model } = harness();
  const context = { requestId: 'nonce', sessionId: 'p1', viewVersion: 0,
    serverReply: { id: 10, role: 'assistant', content: 'saved', session_id: 'p1', request_id: 'nonce', partial: false } };
  model.agentChatContext = context;
  model.receiveAgentEvent(event(1, 'started'), context);
  model.receiveAgentEvent(event(2, 'completed'), context);
  const reply = model.preserveChatReply(context, { partial: false });
  assert.equal(reply.agent_run_id, 'r1');
  assert.equal(reply.generation_status, 'completed');
  model.reconcileChatMessage({ id: 10, role: 'assistant', session_id: 'p1', content: 'new saved', agent_run_id: undefined, generation_status: undefined }, true);
  assert.equal(reply.agent_run_id, 'r1');
  assert.equal(reply.generation_status, 'completed');
});

test('timer entry points require actual timer events in the displayed task', () => {
  const { model } = harness();
  assert.equal(model.agentHasTimerEvents({ events: [{ type: 'tool_call', tool_name: 'bash' }] }), false);
  assert.equal(model.agentHasTimerEvents({ events: [{ type: 'status', tool_name: 'timer_create' }] }), false);
  assert.equal(model.agentHasTimerEvents({ events: [{ type: 'tool_call', tool_name: 'mcp__shikigami__timer_create' }] }), true);
  assert.equal(model.agentHasTimerEvents({ events: [{ type: 'tool_result', tool_name: 'timer_list' }] }), true);
  assert.equal(model.agentHasTimerEvents(undefined), false);
});

test('completed task labels retain tool failure evidence even after a successful later call', () => {
  const { model } = harness();
  const run = { state: 'completed', events: [
    { type: 'tool_result', is_error: true },
    { type: 'tool_call', call_id: 'timer1', tool_name: 'timer_list' },
    { type: 'tool_result', call_id: 'timer1', is_error: false },
  ] };
  assert.equal(model.agentRunLabel(run), 'Run ended · a tool call failed');
  assert.equal(model.agentRunHadToolError(run), true);
  assert.equal(model.agentRunLabel({ ...run, state: 'cancelled' }), model.t('agentState_cancelled'));
  assert.equal(model.agentRunLabel({ state: 'completed', events: [] }), 'Task completed');
  assert.equal(model.agentEventLabel({ type: 'tool_result' }), 'Tool result');
  model.locale = 'zh';
  assert.equal(model.agentRunLabel(run), '本轮已结束 · 工具调用曾失败');
});

test('expanded terminal events preserve tool failures and hide protocol-only completion payload', () => {
  const { model } = harness();
  const run = { state: 'completed', events: [{ type: 'tool_result', is_error: true }] };
  const terminal = { type: 'completed', reason: { kind: 'completed' } };
  assert.equal(model.agentEventLabel(terminal, run), model.agentRunLabel(run));
  assert.equal(model.agentEventText(terminal), '');
  assert.notEqual(model.agentEventLabel({ type: 'status', status: 'idle' }), 'idle');
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  assert.ok(taskCard.template.includes('eventLabel(event, run)'));
});

test('task UI hides implementation IDs and gates the timer summary button', () => {
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  assert.doesNotMatch(html, /\{\{\s*(?:run\.parent_run_id|agentContinueRunId)\s*\}\}/);
  assert.match(taskCard.template, /<button[^>]*v-if="hasTimers"[^>]*>/);
  assert.ok(html.includes(':has-timers="agentHasTimerEvents(run)"'));
  const { model } = harness();
  assert.equal(model.t('agentParentTask'), 'Continues a previous task');
  model.locale = 'zh';
  assert.equal(model.t('agentParentTask'), '接续之前的任务');
});

test('global stop after detach cancels every same-profile task then aborts the plain foreground, never other profiles', async () => {
  const streams = [], controls = [];
  const tick = () => new Promise(resolve => setImmediate(resolve));
  const frame = values => ({ done: false, value: new TextEncoder().encode(values.map(v => 'data: ' + JSON.stringify(v) + '\n\n').join('')) });
  const { model } = harness(async (url, options) => {
    if (url.endsWith('/cancel')) return new Promise(resolve => controls.push({ url, body: JSON.parse(options.body), resolve }));
    assert.ok(url.endsWith('/chat'));
    const stream = { signal: options.signal, body: JSON.parse(options.body) }; streams.push(stream);
    return { ok: true, body: { getReader: () => ({ read: () => new Promise((resolve, reject) => {
      stream.read = resolve;
      options.signal.addEventListener('abort', () => reject(new DOMException('Stopped', 'AbortError')), { once: true });
    }) }) } };
  }, { reactive: true });
  const old = model.sendMessage(); await tick();
  streams[0].read(frame([{ agent_event: event(1, 'started', { request_id: 'r1' }) }, { token: 'Task prefix' }])); await tick();
  model.detachAgentChat();
  model.inputText = 'plain follow-up';
  const foreground = model.sendMessage(); await tick();
  assert.equal(streams[1].body.conversation_only, true);
  model.receiveAgentEvent(event(1, 'started', { run_id: 'same-profile-extra' }));
  model.receiveAgentEvent(event(1, 'started', { run_id: 'other-profile', profile_id: 'p2', session_id: 'p2' }));
  const stopping = model.abortChat(); await tick();
  assert.equal(controls.length, 2);
  assert.ok(controls.every(control => control.body.profile_id === 'p1' && !control.url.includes('other-profile')));
  assert.equal(streams[1].signal.aborted, false);
  controls[0].resolve({ ok: true, json: async () => ({ accepted: true, state: 'cancelling' }) }); await tick();
  assert.equal(streams[1].signal.aborted, false, 'wait for every control response');
  controls[1].resolve({ ok: true, json: async () => ({ accepted: true, state: 'cancelling' }) });
  await stopping; await foreground;
  assert.equal(streams[1].signal.aborted, true);
  assert.equal(streams[0].signal.aborted, false, 'background is stopped by its task control, not transport abort');
  assert.equal(Object.values(model.agentRuns).find(run => run.profile_id === 'p2').cancelling, undefined);
  streams[0].read(frame([{ agent_event: event(2, 'cancelled') }, { done: true, cancelled: true }])); await tick();
  streams[0].read({ done: true }); await old;
});

test('global stop waits for an individual in-flight cancellation and rejected controls leave foreground retryable', async () => {
  const { model } = harness();
  model.receiveAgentEvent(event(1, 'started'));
  let resolve, posts = 0, aborts = 0;
  model.agentChatContext = { requestId: 'plain', sessionId: 'p1', viewVersion: 0, runKey: null };
  model._chatAbortController = { abort() { aborts++; } };
  model.agentFetch = () => { posts++; return new Promise(r => { resolve = r; }); };
  const individual = model.cancelAgentRun(Object.values(model.agentRuns)[0]);
  const global = model.abortChat();
  assert.equal(aborts, 0);
  resolve({ accepted: false, state: 'running' });
  await individual; await global;
  assert.equal(posts, 1);
  assert.equal(aborts, 0);
  assert.equal(Object.values(model.agentRuns)[0].cancelling, false);
  model.agentFetch = async () => ({ accepted: true, state: 'cancelling' });
  await model.abortChat();
  assert.equal(aborts, 1);
});

test('oversized metadata avoids impossible previews/downloads but still loads version recovery', async () => {
  const { model } = harness();
  model.agentWorkspaceOpen = true;
  let contentRequests = 0;
  model.agentFetch = async () => ({ versions: [{ id: 'older', snapshot_complete: false, snapshot_omitted_count: 2 }] });
  model.agentWorkspaceResponse = async () => { contentRequests++; throw new Error('Should not fetch oversized content'); };
  await model.selectAgentWorkspaceFile({ path: 'large.txt', size: 100, oversized: true, previewable: false });
  assert.equal(contentRequests, 0);
  assert.match(model.agentWorkspacePreviewHint, /cannot be previewed or downloaded/i);
  assert.equal(model.agentWorkspaceVersions[0].id, 'older');
  await model.downloadAgentWorkspaceFile();
  assert.equal(contentRequests, 0);
  assert.match(model.agentWorkspaceVersionHint(model.agentWorkspaceVersions[0]), /2/);
  assert.equal(model.agentWorkspaceVersionHint({ snapshot_complete: true, snapshot_omitted_count: 0 }), '');
  assert.match(model.agentWorkspaceVersionHint({ snapshot_complete: false, snapshot_omitted_count: 0 }), /not include all/i);
});

test('unpreviewable metadata is respected independently of file size', async () => {
  const { model } = harness();
  model.agentWorkspaceOpen = true;
  model.agentFetch = async () => ({ versions: [] });
  model.agentWorkspaceResponse = async () => { throw new Error('Unexpected file fetch'); };
  await model.selectAgentWorkspaceFile({ path: 'unsupported.bin', size: 1, previewable: false });
  assert.equal(model.agentWorkspaceError, '');
  assert.match(model.agentWorkspacePreviewHint, /preview unavailable/i);
});

test('bounded and over-limit inventories have readable hints and cannot masquerade as an empty workspace', async () => {
  const { model } = harness();
  model.agentWorkspaceOpen = true;
  model.agentFetch = async () => ({ files: [], truncated: true, over_byte_limit: true, total_bytes_is_lower_bound: true });
  await model.refreshAgentWorkspace();
  assert.match(model.agentWorkspaceInventoryHint, /only part/i);
  assert.match(model.agentWorkspaceInventoryHint, /size limit/i);
  model.closeAgentWorkspace();
  assert.equal(model.agentWorkspaceInventoryHint, '');
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  const empty = html.split('\n').find(line => line.includes("{{ t('agentWorkspaceEmpty') }}"));
  assert.ok(empty.includes('!agentWorkspaceInventoryHint'));
});

test('snapshot warning stays a warning on a completed task and survives run polling', async () => {
  const { model, sandbox } = harness();
  model.receiveAgentEvent(event(1, 'completed', { workspace_warning: 'Some files could not be saved.' }));
  let run = Object.values(model.agentRuns)[0];
  assert.equal(run.state, 'completed');
  assert.equal(run.workspace_warning, 'Some files could not be saved.');
  assert.equal(model.currentAgentRuns.length, 1);
  model.agentFetch = async () => ({ runs: [{ id: 'r1', profile_id: 'p1', session_id: 'p1', state: 'completed', workspace_warning: 'Saved only some files.' }] });
  await sandbox.AgentUIMixin.methods.refreshAgentRuns.call(model);
  run = Object.values(model.agentRuns)[0];
  assert.equal(run.workspace_warning, 'Saved only some files.');
  assert.equal(run.state, 'completed');
  assert.equal(model.agentRunLabel(run), 'Task completed');
});

test('web settings disclose hostname-only Google DNS fallback in both languages', () => {
  const { model } = harness();
  for (const locale of ['zh', 'en']) {
    model.locale = locale;
    const hint = model.t('agentWebDnsHint');
    assert.match(hint, /Google/);
    assert.match(hint, /FakeIP/);
    assert.match(hint, locale === 'en' ? /hostname/ : /主机名/);
    assert.match(hint, locale === 'en' ? /file contents/ : /文件内容/);
  }
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  assert.ok(html.includes("{{ t('agentWebDnsHint') }}"));
  assert.ok(taskCard.template.includes('{{ run.workspace_warning }}'));
});

test('refresh after replacing an oversized file updates its selected preview and download eligibility', async () => {
  const { model } = harness();
  model.agentWorkspaceOpen = true;
  model.agentWorkspaceSelected = { path: 'repaired.txt', size: 20000000, oversized: true, previewable: false };
  model.agentWorkspacePreviewHint = model.agentWorkspaceFileHint(model.agentWorkspaceSelected);
  model.agentFetch = async url => url.includes('/files?') ?
    { files: [{ path: 'repaired.txt', size: 5, oversized: false, previewable: true }] } : { versions: [] };
  model.agentWorkspaceResponse = async () => ({ blob: async () => new Blob(['fixed'], { type: 'text/plain' }) });
  await model.refreshAgentWorkspace();
  assert.equal(model.agentWorkspaceSelected.oversized, false);
  assert.equal(model.agentWorkspacePreviewHint, '');
  assert.equal(model.agentWorkspaceText, 'fixed');
});
