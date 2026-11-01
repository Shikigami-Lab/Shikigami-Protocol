const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const jsDir = path.join(__dirname, '../static/js');
const vueContext = vm.createContext({ console });
vm.runInContext(fs.readFileSync(path.join(jsDir, '../libs/vue.global.prod.js'), 'utf8'), vueContext);
const Vue = vueContext.Vue;
const flush = () => new Promise(resolve => setImmediate(resolve));
const response = todos => ({ ok: true, json: async () => ({ todos }) });
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};

function harness(fetchImpl = async () => response([])) {
  let app;
  const sandbox = {
    console, crypto: require('node:crypto').webcrypto, fetch: fetchImpl,
    localStorage: { getItem: () => null, setItem() {} },
    location: { origin: 'http://fixture.test', protocol: 'http:', hostname: 'fixture.test' },
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    marked: { setOptions() {} }, document: { addEventListener() {} },
    Vue: { createApp(value) { app = value; return { component() {}, mount() {} }; } },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  for (const file of ['api-paths.js', 'i18n.js', 'agent-ui.js', 'settings.js', 'compose-assist.js', 'app.js']) {
    vm.runInContext(fs.readFileSync(path.join(jsDir, file), 'utf8'), sandbox);
  }
  const model = Vue.reactive(Object.assign({}, ...app.mixins.map(m => m.data()), app.data(), {
    currentSessionId: 'a', currentGroupId: null, locale: 'en',
  }));
  for (const source of [...app.mixins, app]) {
    for (const [name, method] of Object.entries(source.methods || {})) model[name] = method.bind(model);
    for (const [name, getter] of Object.entries(source.computed || {})) {
      Object.defineProperty(model, name, { configurable: true, get: () => getter.call(model) });
    }
  }
  // Register the actual App watchers using Vue, including their flush semantics.
  const stops = Object.entries(app.watch).map(([name, config]) => {
    const handler = typeof config === 'function' ? config : config.handler;
    return Vue.watch(() => model[name], handler.bind(model), typeof config === 'function' ? {} : config);
  });
  model.$nextTick = async callback => { if (callback) callback(); };
  model.refreshTimers = async () => {};
  model.refreshAgentRuns = async () => {};
  model.showToast = () => {};
  return { model, sandbox, dispose: () => stops.forEach(stop => stop()) };
}

test('tabs split explicit AI tasks from user and legacy tasks', async () => {
  const { model } = harness(async () => response([
    { id: 'legacy', content: 'Legacy' }, { id: 'u', owner: 'user', content: 'Yours' },
    { id: 'ai', owner: 'ai', content: 'Plan', done: false },
  ]));
  await model.refreshTodos();
  assert.equal(model.todoOwner, 'user');
  assert.deepEqual(Array.from(model.visibleTodos, item => item.id), ['legacy', 'u']);
  model.selectTodoOwner('ai');
  assert.deepEqual(Array.from(model.visibleTodos, item => item.id), ['ai']);
  model.selectTodoOwner('external');
  assert.equal(model.todoOwner, 'ai');
});

test('plans drawer and quick popup share REST, owner, draft and stale-view guards', async () => {
  const calls = [];
  const { model } = harness(async (url, options) => { calls.push({ url, options }); return response([{ id: 'ai', owner: 'ai', source_path: 'notes/plan.md', evidence_path: '../escape' }]); });
  model.refreshAgentWorkspace = async () => {};
  await model.openAgentWorkspace('plans'); await flush();
  assert.equal(model.agentWorkspaceTab, 'plans');
  assert.equal(calls[0].url, 'http://fixture.test/sessions/a/todos');
  model.selectTodoOwner('ai'); model.newTodoText = 'Shared draft';
  model.toggleTodosPanel(); await flush();
  assert.equal(model.newTodoText, 'Shared draft');
  assert.equal(model.visibleTodos[0].id, 'ai');
  assert.equal(model.todoArtifactPath(model.todos[0], 'evidence_path'), '');
  const oldTodo = model.todos[0];
  model.currentSessionId = 'b'; model.currentSessionId = 'a';
  assert.equal(model.todoArtifactPath(oldTodo, 'source_path'), '');
  model.currentGroupId = 'g';
  const count = calls.length;
  await model.openAgentWorkspace('plans'); await model.refreshTodos(); await model.addTodo();
  assert.equal(calls.length, count);
});

test('successful todo tools refresh an open plans tab even with quick popup disabled', async () => {
  const { model } = harness(); let refreshes = 0;
  model.agentWorkspaceOpen = true; model.agentWorkspaceTab = 'plans';
  model.toolbarConfig.todos.enabled = false;
  model.refreshTodos = async () => { refreshes++; };
  model.receiveAgentEvent({ run_id: 'r', profile_id: 'a', session_id: 'a', sequence: 1, type: 'tool_result', tool_name: 'todo_add', is_error: false });
  await flush(); assert.equal(refreshes, 1);
});

test('metadata saves share synchronous role invalidation across A-B-A in one tick', async () => {
  const { model } = harness(); const pending = deferred(); let writes = 0;
  model.agentWorkspaceOpen = true; model.agentRunsKnown.a = true;
  model.agentWorkspaceSelected = { path: 'notes/plan.md' };
  model.agentWorkspaceMetadata = { description: 'Before switch', category: 'note', pinned: false };
  model.agentFetch = async (url, options) => { if (options) writes++; return pending.promise; };
  const saving = model.saveAgentWorkspaceMetadata();
  model.currentSessionId = 'b'; model.currentSessionId = 'a';
  pending.resolve({ runs: [] }); await saving;
  assert.equal(writes, 0);
});

test('add snapshots owner at submit and preserves a subsequently edited draft', async () => {
  const calls = [], pending = deferred();
  const { model } = harness(async (url, options) => {
    calls.push({ url, options });
    return options?.method === 'POST' ? pending.promise : response([]);
  });
  model.selectTodoOwner('ai'); model.newTodoText = 'Plan';
  const adding = model.addTodo();
  assert.deepEqual(JSON.parse(calls[0].options.body), { content: 'Plan', owner: 'ai' });
  model.selectTodoOwner('user'); model.newTodoText = 'My next task';
  pending.resolve(response([])); await adding;
  assert.equal(model.newTodoText, 'My next task');
  assert.equal(calls[0].url, 'http://fixture.test/sessions/a/todos');
  await model.addTodo();
  assert.equal(JSON.parse(calls[2].options.body).owner, 'user');
});

test('A-B-A invalidates old loads even before Vue nextTick, including delayed JSON', async () => {
  const oldJson = deferred(); let count = 0;
  const { model } = harness(async () => ++count === 1 ? { ok: true, json: () => oldJson.promise } : response([{ id: 'fresh' }]));
  const oldLoad = model.refreshTodos(); await flush();
  model.currentSessionId = 'b'; model.currentSessionId = 'a';
  await model.refreshTodos();
  oldJson.resolve({ todos: [{ id: 'stale' }] }); await oldLoad;
  assert.deepEqual(Array.from(model.todos, item => item.id), ['fresh']);
});

test('newer load wins when same-view requests complete out of order', async () => {
  const pending = deferred(); let count = 0;
  const { model } = harness(async () => ++count === 1 ? pending.promise : response([{ id: 'new' }]));
  const older = model.refreshTodos(); await model.refreshTodos();
  pending.resolve(response([{ id: 'old' }])); await older;
  assert.equal(model.todos[0].id, 'new');
});

for (const action of ['add', 'toggle', 'delete', 'clear']) {
  test(`stale ${action} completion cannot refresh or clear the new role draft`, async () => {
    const pending = deferred(), calls = [];
    const { model } = harness(async (url, options) => {
      calls.push({ url, options });
      return options ? pending.promise : response([{ id: 'x', owner: 'ai', done: true }]);
    });
    await model.refreshTodos(); model.selectTodoOwner('ai'); model.newTodoText = 'Old';
    const mutation = action === 'add' ? model.addTodo() : action === 'toggle' ? model.toggleTodo('x') :
      action === 'delete' ? model.deleteTodo('x') : model.clearDoneTodos();
    model.currentSessionId = 'b'; model.currentSessionId = 'a';
    model.newTodoText = 'New draft';
    pending.resolve(response([])); await mutation;
    assert.equal(model.newTodoText, 'New draft');
    assert.equal(model.todos.length, 0);
    assert.equal(calls.length, 2, 'stale mutation must not trigger GET in a later view');
    assert.match(calls[1].url, /\/sessions\/a\/todos/);
  });
}

test('clear completed is scoped to the current tab; failed mutations do not refresh', async () => {
  const calls = [];
  const { model } = harness(async (url, options) => {
    calls.push({ url, options });
    return options ? { ok: false, status: 500 } : response([{ id: 'x', owner: 'ai', done: true }]);
  });
  await model.refreshTodos(); model.selectTodoOwner('ai');
  await model.clearDoneTodos(); await model.toggleTodo('x'); await model.deleteTodo('x');
  assert.equal(calls[1].url, 'http://fixture.test/sessions/a/todos?owner=ai');
  assert.equal(calls[1].options.method, 'DELETE');
  assert.equal(calls.length, 4);
  assert.equal(model.todos.length, 1);
});

test('group entry clears task state and every task action is inert in groups', async () => {
  const calls = [];
  const { model } = harness(async (...args) => { calls.push(args); return response([{ id: 'x' }]); });
  await model.refreshTodos(); model.showTodosPanel = true; model.newTodoText = 'Draft'; model.todoOwner = 'ai';
  model.currentGroupId = 'g';
  assert.equal(model.showTodosPanel, false);
  assert.equal(model.todos.length, 0);
  assert.equal(model.newTodoText, '');
  assert.equal(model.todoOwner, 'user');
  model.toggleTodosPanel(); model.newTodoText = 'Blocked';
  await model.refreshTodos(); await model.addTodo(); await model.toggleTodo('x');
  await model.deleteTodo('x'); await model.clearDoneTodos();
  assert.equal(calls.length, 1);
  assert.equal(model.showTodosPanel, false);
});

test('plan files use the owning profile scoped artifact opener and reject stale or external metadata', async () => {
  const { model } = harness(async () => response([
    { id: 'p', owner: 'ai', source_path: 'notes/plan.md', evidence_path: 'evidence/run.txt', last_run_id: 'r' },
  ]));
  await model.refreshTodos();
  const todo = model.todos[0], opened = [];
  model.openAgentArtifact = artifact => opened.push({ artifact, profile: model.currentSessionId });
  await model.openTodoArtifact(todo, 'source_path'); await model.openTodoArtifact(todo, 'evidence_path');
  assert.deepEqual(opened.map(item => item.artifact.path), ['notes/plan.md', 'evidence/run.txt']);
  assert.ok(opened.every(item => item.profile === 'a' && item.artifact.profile_id === 'a'));
  for (const bad of ['file:///C:/secret', 'https://example.org/x', '/etc/passwd', '../secret', 'C:\\secret']) {
    todo.source_path = bad; await model.openTodoArtifact(todo, 'source_path');
  }
  await model.openTodoArtifact(todo, 'last_run_id');
  todo.source_path = 'notes/plan.md';
  model.currentSessionId = 'b'; await model.openTodoArtifact(todo, 'source_path');
  model.currentSessionId = 'a'; await model.openTodoArtifact(todo, 'source_path');
  assert.equal(opened.length, 2);
});

test('successful own todo tool results refresh an open popup through existing event/poll ingestion', async () => {
  const { model } = harness(); let refreshes = 0;
  model.refreshTodos = async () => { refreshes++; };
  model.showTodosPanel = true;
  const event = (sequence, extra = {}) => ({ run_id: 'r', profile_id: 'a', session_id: 'a',
    sequence, type: 'tool_result', tool_name: 'mcp__shikigami__todo_add', is_error: false, ...extra });
  model.receiveAgentEvent(event(1)); await flush();
  assert.equal(refreshes, 1);
  model.receiveAgentEvent(event(1)); await flush();
  model.receiveAgentEvent(event(2, { is_error: true }));
  model.receiveAgentEvent(event(3, { is_error: undefined }));
  model.receiveAgentEvent(event(4, { tool_name: 'timer_list' }));
  model.receiveAgentEvent(event(5, { profile_id: 'b', session_id: 'b' })); await flush();
  assert.equal(refreshes, 1);
  model.receiveAgentEvent(event(6, { tool_name: 'todo_complete' })); await flush();
  assert.equal(refreshes, 2);
  model.showTodosPanel = false;
  model.receiveAgentEvent(event(7)); await flush();
  assert.equal(refreshes, 2);
  model.currentGroupId = 'g'; model.receiveAgentEvent(event(8)); await flush();
  assert.equal(refreshes, 2);
});

test('todo capability defaults off through older load/save responses and respects profile restrictions', async () => {
  const { model } = harness();
  assert.ok(model.agentCapabilities.includes('todo_enabled'));
  assert.equal(model.agentCapabilities.length, 5);
  assert.equal(model.agentSettingsForm.todo_enabled, false);
  model.agentFetch = async () => ({});
  await model.loadAgentSettings(); assert.equal(model.agentSettingsForm.todo_enabled, false);
  await model.saveAgentSettings({ enabled: true }); assert.equal(model.agentSettingsForm.todo_enabled, false);
  model.profiles = [{ profile_id: 'a', tool_configs: { agent: { todo_enabled: false }, todo: { enabled: false } } }];
  model.selectedProfileId = 'a';
  assert.equal(model.getProfileAgentPermission('todo_enabled'), false);
  assert.equal(model.profiles[0].tool_configs.todo.enabled, false);
  assert.equal(model.t('agentCapability_todo_enabled'), 'Tasks and plans');
});

test('default-user add and existing PATCH/delete contracts refresh after success', async () => {
  const calls = [];
  const { model } = harness(async (url, options) => {
    calls.push({ url, options });
    return response([{ id: 'user', content: 'Mine', done: false }]);
  });
  model.newTodoText = ' Mine ';
  await model.addTodo();
  assert.deepEqual(JSON.parse(calls[0].options.body), { content: 'Mine', owner: 'user' });
  assert.equal(model.newTodoText, '');
  await model.toggleTodo('user');
  assert.equal(calls[2].options.method, 'PATCH');
  assert.deepEqual(JSON.parse(calls[2].options.body), { done: true });
  assert.equal(calls[2].url, 'http://fixture.test/sessions/a/todos/user');
  await model.deleteTodo('user');
  assert.equal(calls[4].options.method, 'DELETE');
  assert.equal(calls.length, 6);
  model.selectTodoOwner('ai');
  await model.toggleTodo('user'); await model.deleteTodo('user');
  assert.equal(calls.length, 6, 'hidden-owner IDs cannot be mutated from this tab');
});

test('clear snapshots each owner, refreshes after success and leaves pending rows untouched locally', async () => {
  const calls = [];
  const { model } = harness(async (url, options) => {
    calls.push({ url, options });
    return response([{ id: 'u', done: true }, { id: 'a', owner: 'ai', done: true }]);
  });
  await model.refreshTodos(); await model.clearDoneTodos();
  model.selectTodoOwner('ai'); await model.clearDoneTodos();
  assert.equal(calls[1].url, 'http://fixture.test/sessions/a/todos?owner=user');
  assert.equal(calls[3].url, 'http://fixture.test/sessions/a/todos?owner=ai');
  assert.equal(calls.length, 5);
});

test('existing poll replays successful own todo results once without refreshing foreign runs', async () => {
  const { model, sandbox } = harness(); let refreshes = 0;
  model.showTodosPanel = true;
  model.refreshTodos = async () => { refreshes++; };
  const event = { run_id: 'r', profile_id: 'a', session_id: 'a', sequence: 1,
    type: 'tool_result', tool_name: 'todo_update', is_error: false };
  model.agentFetch = async () => ({ runs: [{ ...event, id: 'r', state: 'running', events: [event] }] });
  const poll = sandbox.AgentUIMixin.methods.refreshAgentRuns.bind(model);
  await poll(); await flush(); assert.equal(refreshes, 1);
  await poll(); await flush(); assert.equal(refreshes, 1);
});

test('real artifact opener captures profile before listing and rejects late A-B-A listing', async () => {
  const { model } = harness(async () => response([{ id: 'a', owner: 'ai', source_path: 'notes/plan.md' }]));
  await model.refreshTodos();
  const calls = [], pending = deferred();
  model.agentFetch = url => { calls.push(url); return pending.promise; };
  let previews = 0;
  model.selectAgentWorkspaceFile = async () => { previews++; };
  const opening = model.openTodoArtifact(model.todos[0], 'source_path');
  assert.equal(calls[0], '/api/agent/workspace/files?profile_id=a');
  model.currentSessionId = 'b'; model.agentViewChanged();
  model.currentSessionId = 'a'; model.agentViewChanged();
  pending.resolve({ files: [{ path: 'notes/plan.md' }] }); await opening;
  assert.equal(previews, 0);
  assert.equal(model.agentWorkspaceSelected, null);
});

test('popup labels and dynamic controls reuse existing templates in both locales', () => {
  const { model } = harness();
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  const sandbox = { window: {} };
  vm.runInNewContext(fs.readFileSync(path.join(jsDir, 'components.js'), 'utf8'), sandbox);
  const component = sandbox.window.SK_COMPONENTS['todo-list'];
  assert.equal((html.match(/<todo-list /g) || []).length, 2);
  assert.equal((html.match(/:todos="visibleTodos"/g) || []).length, 2);
  assert.equal((html.match(/@artifact="openTodoArtifact"/g) || []).length, 2);
  assert.equal((html.match(/@clear="clearDoneTodos"/g) || []).length, 2);
  assert.match(component.template, /v-for="todo in todos"/);
  assert.match(component.template, /artifactPath\(todo, 'source_path'\)/);
  assert.match(component.template, /artifactPath\(todo, 'evidence_path'\)/);
  for (const locale of ['en', 'zh']) {
    model.locale = locale;
    for (const key of ['todosOwnerUser', 'todosOwnerAi', 'todosSource', 'todosEvidence', 'todosClearDone', 'agentCapability_todo_enabled',
      'agentWorkspacePlansTab', 'agentWorkspaceDescription', 'agentWorkspaceCategory', 'agentWorkspacePinned', 'agentWorkspaceMetadataSave',
      'agentWorkspaceMetadataInvalid', 'agentTool_workspace_describe', 'todosAdd', 'todosDelete',
      ...['note', 'work', 'material', 'other'].map(category => 'agentWorkspaceCategory_' + category)]) {
      assert.notEqual(model.t(key), key);
    }
  }
});

test('shared todo template emits controller actions and guarded source/evidence with the owning row', () => {
  const sandbox = { window: {} };
  vm.runInNewContext(fs.readFileSync(path.join(jsDir, 'components.js'), 'utf8'), sandbox);
  const component = sandbox.window.SK_COMPONENTS['todo-list'];
  const render = Vue.compile(component.template), emitted = [];
  const todo = { id: 'plan', owner: 'ai', content: 'Plan', done: true };
  const tree = render({ t: key => key, owner: 'ai', todos: [todo], draft: '',
    artifactPath: (row, field) => field === 'source_path' ? 'notes/plan.md' : '',
    $emit: (...args) => emitted.push(args) }, []);
  const nodes = [];
  const visit = node => { if (!node || typeof node !== 'object') return; nodes.push(node); if (Array.isArray(node.children)) node.children.forEach(visit); };
  visit(tree);
  const button = label => nodes.find(node => node.type === 'button' && (node.children === label || node.props?.['aria-label'] === label));
  button('todosOwnerUser').props.onClick();
  nodes.find(node => node.type === 'input').props.onInput({ target: { value: 'New draft' } });
  button('todosAdd').props.onClick();
  button('Plan').props.onClick();
  button('todosSource').props.onClick();
  assert.equal(button('todosEvidence'), undefined, 'unsafe/missing evidence stays absent');
  button('todosDelete').props.onClick();
  button('todosClearDone').props.onClick();
  assert.deepEqual(emitted.map(args => Array.from(args)), [
    ['update:owner', 'user'], ['update:draft', 'New draft'], ['add'], ['toggle', 'plan'],
    ['artifact', todo, 'source_path'], ['delete', 'plan'], ['clear'],
  ]);
});

test('todo tool cards localize native and MCP names in both languages', () => {
  const { model } = harness();
  const labels = {
    en: { list: 'List tasks and plans', add: 'Add task or plan', update: 'Update task or plan', delete: 'Delete task or plan' },
    zh: { list: '查看待办与计划', add: '新增待办或计划', update: '更新待办或计划', delete: '删除待办或计划' },
  };
  for (const [locale, actions] of Object.entries(labels)) {
    model.locale = locale;
    for (const [action, label] of Object.entries(actions)) {
      assert.equal(model.agentToolLabel('todo_' + action), label);
      assert.equal(model.agentToolLabel('mcp__shikigami__todo_' + action), label);
    }
  }
});

test('closing a popup before queued tool refresh does not start a request', async () => {
  const calls = [];
  const { model } = harness(async (...args) => { calls.push(args); return response([]); });
  model.showTodosPanel = true;
  model.receiveAgentEvent({ run_id: 'r', profile_id: 'a', session_id: 'a', sequence: 1,
    type: 'tool_result', tool_name: 'todo_add', is_error: false });
  model.showTodosPanel = false;
  await flush();
  assert.equal(calls.length, 0);
  assert.equal(model.showTodosPanel, false);
});

test('closed-popup load cannot overwrite a newer reopened popup or reopen itself', async () => {
  const pending = deferred(); let calls = 0;
  const { model } = harness(async () => ++calls === 1 ? pending.promise : response([{ id: 'current' }]));
  model.showTodosPanel = true;
  const oldLoad = model.refreshTodos();
  model.showTodosPanel = false;
  model.toggleTodosPanel(); await flush();
  pending.resolve(response([{ id: 'old' }])); await oldLoad;
  assert.equal(model.todos[0].id, 'current');
  model.showTodosPanel = false; await flush();
  assert.equal(model.showTodosPanel, false);
});

test('native call_id-only todo results refresh the visible list after their matching call', async () => {
  for (const toolName of ['todo_add', 'mcp__shikigami__todo_add']) {
    const calls = [];
    const { model } = harness(async url => {
      calls.push(url);
      return response([{ id: 'created', owner: 'ai', content: 'Native plan' }]);
    });
    model.showTodosPanel = true; model.selectTodoOwner('ai');
    const binding = { run_id: 'native', profile_id: 'a', session_id: 'a', turn: 'turn-1', step: 1 };
    model.receiveAgentEvent({ ...binding, sequence: 1, type: 'tool_call',
      call_id: 'todo-call', tool_name: toolName, arguments: '{"content":"Native plan"}' });
    await flush(); assert.equal(calls.length, 0);
    // controls.mjs emits call_id/is_error/content, without tool_name.
    const result = { ...binding, sequence: 2, type: 'tool_result', call_id: 'todo-call',
      is_error: false, content: [{ type: 'text', text: '{"ok":true}' }] };
    model.receiveAgentEvent(result); await flush();
    assert.deepEqual(calls, ['http://fixture.test/sessions/a/todos']);
    assert.equal(model.visibleTodos[0].id, 'created');
    model.receiveAgentEvent(result); await flush();
    assert.equal(calls.length, 1, 'duplicate results do not refresh twice');
  }
});

test('persisted native results without tool_name refresh through actual run polling once', async () => {
  const calls = [];
  const { model, sandbox } = harness(async url => {
    calls.push(url); return response([{ id: 'saved', owner: 'ai', content: 'Saved plan' }]);
  });
  model.showTodosPanel = true; model.selectTodoOwner('ai');
  const binding = { run_id: 'persisted', profile_id: 'a', session_id: 'a', request_id: 'persisted' };
  const events = [
    { ...binding, sequence: 1, seq: 1, type: 'tool_call', call_id: 'saved-call', tool_name: 'todo_update', arguments: '{}' },
    { ...binding, sequence: 2, seq: 2, type: 'tool_result', call_id: 'saved-call', is_error: false,
      content: [{ type: 'text', text: 'Updated plan' }] },
    { ...binding, sequence: 3, seq: 3, type: 'completed' },
  ];
  const runRequests = [];
  const snapshots = [{ ...binding, id: 'persisted', state: 'completed', events: [...events].reverse() }];
  model.agentFetch = async url => {
    runRequests.push(url);
    return { runs: snapshots };
  };
  const poll = sandbox.AgentUIMixin.methods.refreshAgentRuns.bind(model);
  await poll(); await flush();
  assert.deepEqual(runRequests, ['/api/agent/runs?profile_id=a']);
  assert.equal(calls.length, 1);
  assert.equal(model.visibleTodos[0].id, 'saved');
  await poll(); await flush(); assert.equal(calls.length, 1);
  model.showTodosPanel = false;
  const closedBinding = { ...binding, run_id: 'closed', request_id: 'closed' };
  snapshots.push({ ...closedBinding, id: 'closed', state: 'completed',
    events: events.map(event => ({ ...event, ...closedBinding })) });
  await poll(); await flush(); assert.equal(calls.length, 1);
});

test('todo result correlation is by prior call_id within its own run, with explicit name only as fallback', async () => {
  const { model } = harness(); let refreshes = 0;
  model.showTodosPanel = true; model.refreshTodos = async () => { refreshes++; };
  const binding = { profile_id: 'a', session_id: 'a' };
  const send = (run, sequence, type, extra) => model.receiveAgentEvent({ ...binding, run_id: run, sequence, type, ...extra });
  send('one', 1, 'tool_call', { call_id: 'shared', tool_name: 'todo_add' });
  send('two', 1, 'tool_call', { call_id: 'shared', tool_name: 'timer_list' });
  send('two', 2, 'tool_result', { call_id: 'shared', is_error: false, tool_name: 'todo_add' });
  send('orphan', 1, 'tool_result', { call_id: 'shared', is_error: false });
  send('future', 1, 'tool_result', { call_id: 'future-call', is_error: false });
  send('future', 2, 'tool_call', { call_id: 'future-call', tool_name: 'todo_add' });
  send('one', 2, 'tool_call', { call_id: 'other', tool_name: 'timer_list' });
  await flush(); assert.equal(refreshes, 0);
  send('one', 3, 'tool_result', { call_id: 'shared', is_error: false });
  await flush(); assert.equal(refreshes, 1, 'interleaved timer call does not replace the matching todo call');
  send('one', 4, 'tool_result', { call_id: 'shared', is_error: true });
  await flush(); assert.equal(refreshes, 1);
  send('fallback', 1, 'tool_result', { call_id: 'missing', tool_name: 'todo_delete', is_error: false });
  await flush(); assert.equal(refreshes, 2);
});

test('only the todo popup opts into narrow-screen viewport bounds and scrolling', () => {
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  const css = fs.readFileSync(path.join(__dirname, '../static/css/settings.css'), 'utf8');
  assert.ok(/class="toolbar-panel tp-todos" v-show="showTodosPanel"/.test(html), 'todo popup needs its own responsive class');
  assert.equal((html.match(/class="toolbar-panel tp-todos"/g) || []).length, 1);
  assert.match(html, /class="toolbar-panel" v-show="showTimerPanel"/);
  const narrow = css.match(/@media\s*\(max-width:\s*768px\)\s*\{\s*\.toolbar-panel\.tp-todos\s*\{([^}]+)\}/);
  assert.ok(narrow, 'todo viewport rule must be scoped to narrow screens');
  assert.match(narrow[1], /position:\s*fixed/);
  for (const edge of ['left', 'right', 'bottom']) assert.match(narrow[1], new RegExp(edge + ':\\s*max\\(12px,\\s*env\\(safe-area-inset-' + edge + '\\)\\)'));
  assert.match(narrow[1], /width:\s*auto/);
  assert.match(narrow[1], /max-height:[^;]*100dvh/);
  assert.match(narrow[1], /overflow-y:\s*auto/);
});
