const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture() {
  const saved = [];
  const sidebar = { width: 100, getBoundingClientRect() { return { width: this.width }; } };
  const app = { clientWidth: 1360, querySelector: () => sidebar };
  const sandbox = {
    document: { getElementById: () => app },
    localStorage: { getItem: () => '610', setItem: (key, value) => saved.push([key, value]) },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/js/agent-ui.js'), 'utf8'), sandbox);
  const mixin = sandbox.AgentUIMixin;
  const state = mixin.data();
  for (const [name, method] of Object.entries(mixin.methods)) state[name] = method.bind(state);
  return { state, app, sidebar, saved };
}

test('workspace keeps a preferred width across viewport and sidebar changes', () => {
  const { state, app, sidebar, saved } = fixture();
  state.loadAgentWorkspaceWidth();
  assert.equal(state.agentWorkspaceWidth, 610);
  app.clientWidth = 1120;
  state.syncAgentWorkspaceWidth();
  assert.equal(state.agentWorkspaceWidth, 500);
  sidebar.width = 0;
  state.syncAgentWorkspaceWidth();
  assert.equal(state.agentWorkspaceWidth, 600);
  app.clientWidth = 1360;
  state.syncAgentWorkspaceWidth();
  assert.equal(state.agentWorkspaceWidth, 610);
  assert.deepEqual(saved, []);
});

test('workspace keyboard width is bounded, remembered and disabled on narrow screens', () => {
  const { state, saved } = fixture();
  state.loadAgentWorkspaceWidth();
  let handled = 0;
  const press = key => state.resizeAgentWorkspaceWithKey({ key, preventDefault: () => handled++ });
  press('End');
  assert.equal(state.agentWorkspaceWidth, 720);
  press('Home');
  assert.equal(state.agentWorkspaceWidth, 320);
  press('ArrowLeft');
  assert.equal(state.agentWorkspaceWidth, 340);
  state.agentDrawerNarrow = true;
  press('ArrowLeft');
  assert.equal(state.agentWorkspaceWidth, 340);
  assert.equal(handled, 3);
  assert.deepEqual(saved.map(row => row[1]), ['720', '320', '340']);
});
