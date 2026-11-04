const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const staticRoot = path.join(__dirname, '../static');
const source = file => fs.readFileSync(path.join(staticRoot, file), 'utf8');
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};

function workspace(document) {
  const sandbox = vm.createContext({ URL, Blob, document });
  sandbox.window = sandbox;
  for (const file of ['libs/marked.min.js', 'js/api-paths.js', 'js/agent-ui.js']) {
    vm.runInContext(source(file), sandbox);
  }
  const mixin = sandbox.AgentUIMixin;
  const model = {
    ...mixin.data(), ...mixin.methods, currentSessionId: 'p1', currentGroupId: null,
    agentWorkspaceOpen: true, t: key => key, $nextTick: fn => fn(),
  };
  for (const [name, get] of Object.entries(mixin.computed)) {
    Object.defineProperty(model, name, { get: () => get.call(model) });
  }
  return model;
}

test('reply cards use only safe, terminal outputs owned by that assistant message', () => {
  const model = workspace();
  const reply = { id: 2, session_id: 'p1', role: 'assistant', agent_run_id: 'r1' };
  const prompt = { id: 1, session_id: 'p1', role: 'user' };
  model.messages = [prompt, reply];
  const run = {
    run_id: 'r1', profile_id: 'p1', session_id: 'p1', state: 'completed',
    user_message_id: 1, assistant_message_id: 2,
    events: [{ type: 'tool_call', arguments: { path: 'attempt.txt' } }],
    artifacts: ['notes/plan.md', { path: 'notes/plan.md' }, { path: 'archive/plan.md' }, '../outside.txt', 'C:/secret', null],
  };
  model.agentRuns = {
    one: run,
    partial: { ...run, run_id: 'r2', state: 'cancelled', artifacts: ['saved.txt'] },
    pending: { ...run, run_id: 'r3', state: 'running', artifacts: ['pending.txt'] },
    other: { ...run, run_id: 'r4', profile_id: 'p2', session_id: 'p2', artifacts: ['private.txt'] },
    reflection: { ...run, run_id: 'r5', origin: 'reflection', artifacts: ['reflection.txt'] },
    wrongSession: { ...run, run_id: 'r6', session_id: 'p2', artifacts: ['other-session.txt'] },
  };
  assert.deepEqual(Array.from(model.messageAgentArtifacts(reply)), ['notes/plan.md', 'archive/plan.md', 'saved.txt']);
  assert.equal(model.messageAgentArtifacts(prompt).length, 0);
  assert.equal(model.messageAgentRuns(reply)[0].events, run.events);
  model.currentGroupId = 'group';
  assert.equal(model.messageAgentArtifacts(reply).length, 0);
});

test('workspace Markdown formats content without raw HTML, embedded requests or unsafe links', () => {
  const model = workspace();
  model.agentWorkspaceSelected = { path: 'notes/PLAN.MD' };
  model.agentWorkspaceText = [
    '# Hike', '', '**meet at 08:30**', '', '- Water', '- Map', '',
    '[Guide](https://fixture.invalid/a?b=1&c=2) [unsafe](javascript:alert%281%29) [local](/api/preferences)',
    '![remote](https://fixture.invalid/track.png) <img src="//fixture.invalid/raw" onerror="alert(1)">',
    '<iframe src="https://fixture.invalid/frame"></iframe>',
    '<a href="https://fixture.invalid" ping="https://fixture.invalid/ping">raw link</a>',
    '[encoded](java&#x73;cript:alert%281%29) [credentials](https://user:pass@fixture.invalid/)',
    '', '```html', '<script>alert(1)</script>', '```',
  ].join('\n');
  const html = model.agentWorkspaceMarkdown;
  assert.match(html, /<h1>Hike<\/h1>/);
  assert.match(html, /<strong>meet at 08:30<\/strong>/);
  assert.match(html, /<li>Water<\/li>/);
  assert.match(html, /href="https:\/\/fixture.invalid\/a\?b=1&amp;c=2"[^>]*rel="noopener noreferrer"/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.equal((html.match(/href=/g) || []).length, 1);
  assert.doesNotMatch(html, /<(?:img|iframe|script)|(?:onerror|ping)=/i);
  model.agentWorkspaceSelected = { path: 'notes/plan.txt' };
  assert.equal(model.agentWorkspaceMarkdown, '');
});

test('preview becomes readable while version history is pending', async () => {
  const model = workspace();
  const versions = deferred();
  model.agentFetch = () => versions.promise;
  model.agentWorkspaceResponse = async () => ({ blob: async () => new Blob(['Preview'], { type: 'text/plain' }) });
  const opening = model.selectAgentWorkspaceFile({ path: 'plan.txt' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(model.agentWorkspaceText, 'Preview');
  assert.equal(model.agentWorkspacePreviewBusy, false);
  assert.equal(model.agentWorkspaceVersions, null);
  versions.resolve({ versions: [{ id: 'first' }] });
  await opening;
  assert.equal(model.agentWorkspaceVersions[0].id, 'first');
});

test('return to files invalidates pending content, versions and artifact opens', async () => {
  const model = workspace();
  const body = deferred(), versions = deferred(), files = deferred();
  model.agentFetch = () => versions.promise;
  model.agentWorkspaceResponse = async () => ({ blob: () => body.promise });
  const preview = model.selectAgentWorkspaceFile({ path: 'first.txt' });
  model.openAgentWorkspace = () => files.promise;
  const artifact = model.openAgentArtifact('second.txt');
  model.returnToAgentWorkspaceFiles();
  body.resolve(new Blob(['old body'], { type: 'text/plain' }));
  versions.resolve({ versions: [{ id: 'old' }] });
  model.agentWorkspaceFiles = [{ path: 'second.txt' }];
  files.resolve();
  await Promise.all([preview, artifact]);
  assert.equal(model.agentWorkspaceSelected, null);
  assert.equal(model.agentWorkspaceText, '');
  assert.equal(model.agentWorkspaceVersions.length, 0);
  assert.equal(model.agentWorkspacePreviewBusy, false);
});

test('a removed preview cannot prevent opening a different delivered file', async () => {
  const model = workspace();
  model.openAgentWorkspace = () => model.refreshAgentWorkspace();
  model.agentWorkspaceSelected = { path: 'removed.txt' };
  model.agentFetch = async url => url.includes('/files')
    ? { files: [{ path: 'new.txt' }] }
    : { versions: [] };
  model.agentWorkspaceResponse = async () => ({ blob: async () => new Blob(['New'], { type: 'text/plain' }) });
  await model.openAgentArtifact('new.txt');
  assert.equal(model.agentWorkspaceSelected?.path, 'new.txt');
  assert.equal(model.agentWorkspaceText, 'New');
});

test('returning to files cancels a restore still waiting on its task-status check', async () => {
  const model = workspace();
  const preflight = deferred();
  model.agentWorkspaceSelected = { path: 'plan.txt' };
  let restores = 0;
  model.agentFetch = async url => {
    if (url.includes('/runs')) return preflight.promise;
    if (url.includes('/restore')) restores++;
    return { files: [{ path: 'plan.txt' }] };
  };
  const restoring = model.restoreAgentWorkspaceVersion({ id: 'v1' });
  model.returnToAgentWorkspaceFiles();
  preflight.resolve({ runs: [] });
  await restoring;
  assert.equal(restores, 0);
  assert.equal(model.agentWorkspaceSelected, null);
  assert.equal(model.agentWorkspaceMutationBusy, false);
});


test('a delayed output opens without taking focus back from the composer', async () => {
  const trigger = {}, composer = {}, close = {};
  const document = { activeElement: trigger };
  const model = workspace(document);
  model.$refs = { agentWorkspaceClose: close };
  const listing = deferred();
  const selections = [];
  model.openAgentWorkspace = () => listing.promise;
  model.selectAgentWorkspaceFile = async (file, options) => selections.push([file.path, options.focus]);
  model.agentWorkspaceFiles = [{ path: 'plan.md' }];
  const opening = model.openAgentArtifact('plan.md');
  document.activeElement = composer;
  listing.resolve();
  await opening;
  assert.deepEqual(selections, [['plan.md', false]]);
});
