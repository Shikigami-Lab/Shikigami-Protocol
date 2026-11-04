const { readFileSync } = require('node:fs');
const assert = require('node:assert/strict');
const { join } = require('node:path');
const { test } = require('node:test');
const { runInNewContext } = require('node:vm');

const read = file => readFileSync(join(__dirname, '../static', file), 'utf8');
const html = read('index.html');
const sidebar = html.slice(html.indexOf('<aside'), html.indexOf('</aside>'));
const header = html.slice(html.indexOf('<header class="chat-header"'), html.indexOf('</header>', html.indexOf('<header class="chat-header"')));
const settings = runInNewContext(read('js/settings.js') + '\nSettingsMixin');
const app = runInNewContext(read('js/app.js') + '\nApp', {
  window: {}, crypto: require('node:crypto').webcrypto, SettingsMixin: {}, AgentUIMixin: {}, ComposeAssistMixin: {},
  document: { addEventListener() {} }, marked: { setOptions() {} }, Vue: { createApp: () => ({ mount() {} }) },
});

test('the compact sidebar owns both columns and only collapsed headers offer shortcuts', () => {
  const css = read('css/styles.css');
  assert.ok(/\.sidebar:not\(\.open\)\s*\{\s*width: 0/.test(css), 'the mounted sidebar owns its collapsed width');
  assert.ok(/\.sidebar-shortcut\.gear-btn\s*\{\s*margin-top: auto/.test(css), 'Settings stays at the bottom');
  assert.match(sidebar, /id="chat-sidebar"/);
  assert.match(sidebar, /popovertarget="sidebar-groups"/);
  assert.match(sidebar, /toggleGroupPin\(g.group_id\)/);
  assert.match(sidebar, /openCreateGroupModal/);
  assert.match(sidebar, /agentActiveTaskCount/);
  assert.match(sidebar, /openMemoryPanel/);
  assert.match(sidebar, /openSettings/);
  assert.match(sidebar, /:inert="!sidebarOpen/);
  assert.match(header, /aria-controls="chat-sidebar"/);
  assert.match(header, /v-if="!sidebarOpen && !currentGroupId && currentSessionId" ref="agentWorkspaceOpener"/);
  assert.match(header, /v-if="!sidebarOpen" ref="settingsTrigger"/);
});

test('current-character Memory opens content for the chat character after another profile was viewed', () => {
  const state = Object.assign(settings.data(), {
    currentSessionId: 'chat-role', selectedProfileId: 'settings-role', memoryProfileId: 'previous-role',
    activeTab: 'memory', memoryPanelTab: 'settings', memoryActiveSubTab: 'maintenance',
    selectMemoryProfile: settings.methods.selectMemoryProfile,
    openSettings() {
      assert.equal(this.activeTab, 'memory');
      assert.equal(this.memoryProfileId, 'chat-role');
      assert.equal(this.memoryContentProfileId, 'chat-role');
      assert.equal(this.memoryPanelTab, 'content');
      assert.equal(this.memoryActiveSubTab, 'facts');
      assert.equal(this.selectedProfileId, 'settings-role');
    },
    $nextTick: callback => callback(),
  });
  settings.methods.openMemoryPanel.call(state);
});

test('avatars have native keyboard activation and retain drag and keyboard reorder', async () => {
  assert.match(sidebar, /<button[^>]*class="sidebar-role-button"/);
  assert.match(sidebar, /:aria-label="session.display_name"/);
  assert.match(sidebar, /:aria-current="!currentGroupId && session.id === currentSessionId/);
  assert.match(sidebar, /@dragstart="sessionDragStart/);
  assert.match(sidebar, /@pointerdown.stop="sessionPointerDown/);
  assert.match(sidebar, /@keydown.alt.up.prevent="moveSidebarRole/);
  assert.match(sidebar, /@keydown.alt.down.prevent="moveSidebarRole/);
  const saved = [];
  const state = {
    sessions: [{ id: 'a' }, { id: 'b' }],
    async saveSessionOrder() { saved.push(this.sessions.map(session => session.id).join(',')); },
    moveSessionUp: app.methods.moveSessionUp,
    moveSessionDown: app.methods.moveSessionDown,
    $nextTick: async callback => callback(),
  };
  let focusCount = 0;
  const button = { focus: () => focusCount++ };
  await app.methods.moveSidebarRole.call(state, { currentTarget: button }, 'a', 'down');
  await app.methods.moveSidebarRole.call(state, { currentTarget: button }, 'a', 'up');
  assert.deepEqual(saved, ['b,a', 'a,b']);
  assert.equal(focusCount, 2);
});
