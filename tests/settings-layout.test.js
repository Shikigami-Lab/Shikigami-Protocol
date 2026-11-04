const { readFileSync } = require('node:fs');
const assert = require('node:assert/strict');
const { join } = require('node:path');
const { test } = require('node:test');
const { runInNewContext } = require('node:vm');

const html = readFileSync(join(__dirname, '../static/index.html'), 'utf8');
const panel = (name, next) => html.slice(
  html.indexOf(`v-if="activeTab === '${name}'"`),
  html.indexOf(`v-if="activeTab === '${next}'"`),
);

test('proactive topic sources and screenshot controls live together in Behavior', () => {
  const models = panel('llm', 'tts');
  const behavior = panel('behavior', 'system');
  for (const field of ['topic_discovery.enabled', 'vlm_for_ase', 'vlm_ase_wait_seconds', 'ase_vlm_mode']) {
    assert.ok(behavior.includes(`reflectionForm.${field}`), field);
    assert.ok(!models.includes(`reflectionForm.${field}`), field);
  }
  assert.ok(models.includes('reflectionForm.vlm_model_preset'));
});

test('Models links to the persona memory extraction editor without changing persona selection', () => {
  const models = panel('llm', 'tts');
  const memory = html.slice(html.indexOf(`v-if="activeTab === 'memory'"`));
  assert.ok(!models.includes('v-model="selectedProfileId"'));
  assert.ok(!models.includes('v-model="memoryTabProfileForm.extraction_llm_preset"'));
  assert.ok(memory.includes('v-model="memoryTabProfileForm.extraction_llm_preset"'));
});

test('autosave feedback belongs to its section and persona', () => {
  const script = readFileSync(join(__dirname, '../static/js/settings.js'), 'utf8');
  const timers = [];
  const mixin = runInNewContext(script + '\nSettingsMixin', { setTimeout: callback => timers.push(callback), clearTimeout() {} });
  const state = mixin.data();
  const { setAutoSaveState, settingsSaveState } = mixin.methods;
  setAutoSaveState.call(state, 'reflection', 'saving');
  setAutoSaveState.call(state, 'profile', 'saved', 'a');
  setAutoSaveState.call(state, 'profile', 'error', 'b');
  assert.equal(settingsSaveState.call(state, 'reflection'), 'saving');
  assert.equal(settingsSaveState.call(state, 'profile', 'a'), 'saved');
  assert.equal(settingsSaveState.call(state, 'profile', 'b'), 'error');
  setAutoSaveState.call(state, 'profile', 'saving', 'a');
  timers[0]();
  assert.equal(settingsSaveState.call(state, 'profile', 'a'), 'saving');
});


test('settings tabs restore their own scroll without overriding direct section navigation', () => {
  const script = readFileSync(join(__dirname, '../static/js/settings.js'), 'utf8');
  const mixin = runInNewContext(script + '\nSettingsMixin');
  const memory = { scrollTop: 225 };
  const body = { scrollTop: 370, querySelector: () => memory };
  const queue = [];
  const state = Object.assign(mixin.data(), {
    settingsOpen: true,
    activeTab: 'llm',
    $refs: { settingsBodyScroll: body },
    $nextTick: callback => queue.push(callback),
    settingsScrollContainer: mixin.methods.settingsScrollContainer,
  });
  const switchTab = mixin.methods.switchSettingsScroll.bind(state);
  switchTab('llm', 'onboarding');
  queue.shift()();
  assert.equal(body.scrollTop, 0);
  state.activeTab = 'onboarding';
  switchTab('onboarding', 'memory');
  queue.shift()();
  assert.equal(body.scrollTop, 370);
  assert.equal(state.settingsTabScroll.memory, 225);
  state.settingsScrollTargetTab = 'llm';
  state.activeTab = 'llm';
  switchTab('llm', 'onboarding');
  assert.equal(queue.length, 0);
  assert.equal(state.settingsScrollTargetTab, '');
});
