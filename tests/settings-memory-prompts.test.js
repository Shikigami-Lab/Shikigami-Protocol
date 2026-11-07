'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { webcrypto } = require('node:crypto');
const { join } = require('node:path');
const { runInNewContext } = require('node:vm');

const read = file => readFileSync(join(__dirname, '../static', file), 'utf8');
const html = read('index.html');
const source = read('js/settings.js');
const appSource = read('js/app.js').split('const vueApp = Vue.createApp(App);')[0];
const { API_PATHS } = runInNewContext(read('js/api-paths.js') + '\nwindow;', { window: {} });
const { LOCALES } = runInNewContext(read('js/i18n.js') + '\nwindow;', { window: {} });
const route = '/settings/memory/prompts';
const extraction = 'extraction_prompt';
const summary = 'day_summary_prompt';
const prompts = (overrides = {}) => ({
  defaults: { [extraction]: 'Extract facts from the conversation.', [summary]: 'Summarize the day.' },
  overrides: { [extraction]: null, [summary]: null, ...overrides },
});
const reply = (data, ok = true) => ({ ok, status: ok ? 200 : 500, json: async () => data });

function harness(fetch) {
  const mixin = runInNewContext(source + '\nSettingsMixin;', {
    API_PATHS, fetch, getBaseUrl: () => '', console,
  });
  const model = Object.assign(mixin.data(), { t: key => key });
  for (const [key, method] of Object.entries(mixin.methods)) model[key] = method.bind(model);
  const toasts = [];
  model.showToast = (...args) => toasts.push(args);
  for (const [key, getter] of Object.entries(mixin.computed)) {
    Object.defineProperty(model, key, { get: getter.bind(model) });
  }
  return { model, watch: mixin.watch, toasts };
}

test('Persona Prompt and the Memory shortcut expose one translated global card without a selected persona', () => {
  const detail = html.slice(html.indexOf('class="profile-detail-col"'), html.indexOf('<!-- end Profiles tab -->'));
  assert.match(detail, /id="persona-prompt-tab"[^>]*@click="profileSubTab = 'prompts'"/);
  assert.match(detail, /<!-- end profile-specific settings -->\s*<collapsible-section v-if="profileSubTab === 'prompts'" id="global-memory-prompts"/);
  const card = detail.match(/<collapsible-section[^>]*id="global-memory-prompts"[\s\S]*?<\/collapsible-section>/)[0];
  assert.match(card, /v-model="memoryPromptDraft\[key\]"/);
  assert.match(card, /:disabled="memoryPromptsBusy"/);
  assert.match(card, /saveMemoryPrompt\(key\)/);
  assert.match(card, /saveMemoryPrompt\(key, true\)/);
  assert.match(card, /@click="loadMemoryPrompts\(\)"/);
  assert.match(html, /id="memory-prompts-jump"[\s\S]*?@click="memPromptExpanded = true; openSettingsSection\('profiles', 'global-memory-prompts', 'prompts'\)"/);
  const memoryPersona = html.slice(html.indexOf('id="memory-extraction-settings"'));
  const shortcut = memoryPersona.indexOf('id="memory-prompts-jump"');
  assert.ok(shortcut >= 0 && shortcut < memoryPersona.indexOf('<fieldset'), 'the shortcut is usable while persona settings are disabled');
  assert.doesNotMatch(html, /memoryProfileForm\.|profileWizardResult\.memory_(extraction|day_summary)_prompt/);
  assert.doesNotMatch(html, /profileWizardDisableMemory|wizardDisableMemory/);
  for (const locale of ['en', 'zh']) {
    for (const key of ['secMemoryExtractPrompts', 'hintGlobalMemoryPrompts', 'memoryPromptsLoadFailed',
      'btnResetMemoryPrompt', 'lblExtractionPrompt', 'lblDaySummaryPrompt']) {
      assert.ok(LOCALES[locale][key], `${locale} ${key}`);
    }
  }
});

test('entering Persona Prompt with no persona loads the built-in text and existing overrides', async () => {
  const calls = [];
  const data = prompts({ [summary]: 'Custom summary' });
  const { model, watch } = harness(async (url, options) => {
    calls.push([url, options?.method ?? 'GET']);
    return reply(data);
  });
  assert.equal(model.selectedProfileId, '');
  model.settingsOpen = true;
  model.activeTab = 'profiles';
  model.profileSubTab = 'prompts';
  model.memPromptExpanded = true;
  assert.equal(model.memoryPromptsVisible, true);
  await watch.memoryPromptsVisible.call(model, true);
  assert.deepEqual(calls, [[route, 'GET']]);
  assert.equal(model.memoryPromptDraft[extraction], data.defaults[extraction]);
  assert.equal(model.memoryPromptDraft[summary], 'Custom summary');
  assert.equal(model.memoryPromptIsDirty(extraction), false);
  assert.equal(model.memoryPromptIsDirty(summary), false);
});

test('save and reset PATCH only their own field and preserve the other unsaved draft', async () => {
  let data = prompts({ [summary]: 'Old summary' });
  const writes = [];
  const { model } = harness(async (url, options) => {
    assert.equal(url, route);
    if (options?.method === 'PATCH') {
      const patch = JSON.parse(options.body);
      writes.push(patch);
      data = prompts({ ...data.overrides, ...patch });
    }
    return reply(data);
  });
  await model.loadMemoryPrompts();
  model.memoryPromptDraft[extraction] = '  Custom extraction\n';
  model.memoryPromptDraft[summary] = 'Unsaved summary draft';
  await model.saveMemoryPrompt(extraction);
  assert.deepEqual(writes, [{ [extraction]: '  Custom extraction\n' }]);
  assert.equal(model.memoryPromptIsDirty(extraction), false);
  assert.equal(model.memoryPromptIsDirty(summary), true);
  assert.equal(model.memoryPromptDraft[summary], 'Unsaved summary draft');
  await model.saveMemoryPrompt(summary, true);
  assert.deepEqual(writes[1], { [summary]: null });
  assert.equal(model.memoryPromptDraft[summary], data.defaults[summary]);
  assert.equal(model.memoryPromptDraft[extraction], '  Custom extraction\n');
});

test('reopening during a save queues a refresh after the write', async () => {
  const calls = [];
  const pending = [];
  const { model, watch } = harness((url, options) => {
    calls.push([url, options?.method ?? 'GET']);
    return new Promise(resolve => pending.push(resolve));
  });
  const loading = model.loadMemoryPrompts();
  assert.equal(model.memoryPromptsBusy, true);
  await model.saveMemoryPrompt(extraction, true);
  assert.deepEqual(calls, [[route, 'GET']]);
  pending.shift()(reply(prompts()));
  await loading;
  model.memoryPromptDraft[extraction] = 'Saved globally';
  const saving = model.saveMemoryPrompt(extraction);
  assert.equal(model.memoryPromptsBusy, true);
  model.selectedProfileId = 'another-persona';
  model.settingsOpen = true;
  model.profileSubTab = 'prompts';
  model.memPromptExpanded = true;
  await watch.memoryPromptsVisible.call(model, true);
  await model.saveMemoryPrompt(extraction);
  assert.deepEqual(calls, [[route, 'GET'], [route, 'PATCH']]);
  pending.shift()(reply(prompts({ [extraction]: 'Saved globally' })));
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls, [[route, 'GET'], [route, 'PATCH'], [route, 'GET']]);
  assert.equal(model.memoryPromptsBusy, true);
  pending.shift()(reply(prompts({ [extraction]: 'Saved globally' })));
  await saving;
  assert.equal(model.memoryPromptsBusy, false);
  assert.equal(model.memoryPromptDraft[extraction], 'Saved globally');
  assert.equal(model.memoryPromptIsDirty(extraction), false);
});

test('reopening the card refreshes clean fields while retaining a draft', async () => {
  let data = prompts();
  const { model, watch } = harness(async () => reply(data));
  model.settingsOpen = true;
  model.profileSubTab = 'prompts';
  model.memPromptExpanded = true;
  await watch.memoryPromptsVisible.call(model, true);
  model.memoryPromptDraft[extraction] = 'Unsaved extraction';
  model.memPromptExpanded = false;
  assert.equal(model.memoryPromptsVisible, false);
  data = prompts({ [summary]: 'Updated externally' });
  model.memPromptExpanded = true;
  await watch.memoryPromptsVisible.call(model, true);
  assert.equal(model.memoryPromptDraft[summary], 'Updated externally');
  assert.equal(model.memoryPromptDraft[extraction], 'Unsaved extraction');
  assert.equal(model.memoryPromptIsDirty(extraction), true);
});

test('reopening after a language change waits for the preference POST before fetching defaults', async () => {
  const calls = [];
  const document = { documentElement: {}, addEventListener() {} };
  let finishLocale;
  let data = prompts();
  const fetch = async (url, options) => {
    calls.push([url, options?.method ?? 'GET']);
    if (url === '/api/preferences') return new Promise(resolve => { finishLocale = resolve; });
    assert.equal(url, route);
    return reply(data);
  };
  const { model, watch } = harness(fetch);
  const app = runInNewContext(appSource + '\nApp;', {
    fetch: (url, options) => fetch(new URL(url).pathname, options), document, crypto: webcrypto,
    localStorage: { setItem() {} }, marked: { setOptions() {} },
    window: { location: { origin: 'http://fixture.test', protocol: 'http:' }, LOCALES },
    SettingsMixin: {}, AgentUIMixin: {}, ComposeAssistMixin: {},
  });
  model.setLocale = app.methods.setLocale.bind(model);
  if (app.methods.persistLocale) model.persistLocale = app.methods.persistLocale.bind(model);
  model.statusText = 'idle';
  model.settingsOpen = true;
  model.profileSubTab = 'prompts';
  model.memPromptExpanded = true;
  await watch.memoryPromptsVisible.call(model, true);
  model.memoryPromptDraft[summary] = 'Keep this draft';
  model.activeTab = 'system';
  model.setLocale('zh');
  model.activeTab = 'profiles';
  const reopened = watch.memoryPromptsVisible.call(model, true);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls, [[route, 'GET'], ['/api/preferences', 'POST']]);
  data = { ...data, defaults: { [extraction]: '中文默认提取', [summary]: '中文默认摘要' } };
  finishLocale(reply({ locale: 'zh' }));
  await reopened;
  assert.deepEqual(calls.at(-1), [route, 'GET']);
  assert.equal(model.memoryPromptDraft[extraction], '中文默认提取');
  assert.equal(model.memoryPromptDraft[summary], 'Keep this draft');
});

test('a failed load is retryable and a failed save retains the draft for retry', async () => {
  const data = prompts();
  const responses = [reply({}, false), reply({ defaults: {}, overrides: {} }), reply(data),
    reply({}, false), reply(prompts({ [extraction]: 'Retry this text' }))];
  const { model, toasts } = harness(async () => responses.shift());
  await model.loadMemoryPrompts();
  assert.equal(model.memoryPromptsError, 'memoryPromptsLoadFailed');
  await model.loadMemoryPrompts();
  assert.equal(model.memoryPromptsError, 'memoryPromptsLoadFailed');
  assert.equal(model.memoryPrompts, null);
  await model.loadMemoryPrompts();
  assert.equal(model.memoryPromptsError, '');
  model.memoryPromptDraft[extraction] = 'Retry this text';
  await model.saveMemoryPrompt(extraction);
  assert.equal(model.memoryPromptsError, 'settingsSaveFailed');
  assert.equal(model.memoryPromptIsDirty(extraction), true);
  assert.deepEqual(toasts, [['settingsSaveFailed', 'error']]);
  await model.saveMemoryPrompt(extraction);
  assert.equal(model.memoryPromptsError, '');
  assert.equal(model.memoryPromptIsDirty(extraction), false);
});

test('the persona wizard ignores legacy memory prompt output and makes no memory settings writes', async () => {
  const calls = [];
  const profile = {
    display_name: 'Wizard profile', base_prompt: 'Base persona', style_constraint: 'Style',
    energy_prompts: { 0: 'Low energy' }, reflection_custom_prompt: 'Reflect',
    memory_extraction_prompt: 'Legacy facts', memory_day_summary_prompt: 'Legacy day',
  };
  const { model } = harness(async (url, options) => {
    calls.push([url, JSON.parse(options.body)]);
    return url === '/profiles/wizard' ? reply({ ok: true, profile }) : reply({ ok: true });
  });
  model.openProfileWizard();
  model.profileWizardDesc = 'Generate a persona.';
  await model.runProfileWizard();
  assert.deepEqual(calls[0][1], {
    description: 'Generate a persona.', disable_emotion: false, disable_affinity: false, disable_reflection: false,
  });
  assert.equal(Object.hasOwn(model.profileWizardResult, 'memory_extraction_prompt'), false);
  assert.equal(Object.hasOwn(model.profileWizardResult, 'memory_day_summary_prompt'), false);
  await model._saveWizardToProfile('persona');
  assert.deepEqual(calls.slice(1), [
    ['/profiles/persona', {
      display_name: 'Wizard profile', base_prompt: 'Base persona', style_constraint: 'Style',
      energy_prompts: { 0: 'Low energy' },
    }],
    ['/profiles/persona/reflection_config', { custom_prompt: 'Reflect' }],
  ]);
  assert.equal(Object.hasOwn(model, 'profileWizardDisableMemory'), false);
});

test('Prompt Autofill keeps the existing non-memory scopes and leaves the shared prompts alone', async () => {
  let body;
  const data = prompts();
  const { model } = harness(async (url, options) => {
    if (url === route) return reply(data);
    body = JSON.parse(options.body);
    return reply({ ok: true, patch: {
      style_constraint: 'Keep it short',
      memory_extraction_prompt: 'Legacy facts',
      memory_day_summary_prompt: 'Legacy day',
    } });
  });
  await model.loadMemoryPrompts();
  model.profileForm.profile_id = 'persona';
  model.profileForm.base_prompt = 'A long enough persona description.';
  await model.runPromptAutofill();
  assert.deepEqual(body.scopes, ['emotion', 'emotion_zh', 'energy', 'affinity', 'reflection', 'style', 'anchor']);
  assert.equal(body.fill_empty_only, true);
  assert.equal(Object.hasOwn(body.snapshot, 'memory_extraction_prompt'), false);
  assert.equal(Object.hasOwn(body.snapshot, 'memory_day_summary_prompt'), false);
  assert.equal(model.profileForm.style_constraint, 'Keep it short');
  assert.deepEqual(model.memoryPrompts, data);
  assert.equal(model.memoryPromptDraft[extraction], data.defaults[extraction]);
  assert.equal(model.memoryPromptDraft[summary], data.defaults[summary]);
});
