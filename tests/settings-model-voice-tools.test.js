const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function settings(fetch) {
  const sandbox = vm.createContext({ fetch, console, getBaseUrl: () => 'http://fixture.test',
    setTimeout: () => 0, clearTimeout() {} });
  sandbox.window = sandbox;
  for (const file of ['api-paths.js', 'settings.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js', file), 'utf8'), sandbox);
  }
  const mixin = vm.runInContext('SettingsMixin', sandbox);
  const toasts = [];
  const model = Object.assign(mixin.data(), {
    t: key => key === 'llmPresetDependencyWarning' ? 'Affected settings: {roles}' : key,
  });
  for (const [name, method] of Object.entries(mixin.methods)) model[name] = method.bind(model);
  model.showToast = (text, type) => toasts.push([text, type]);
  for (const [name, getter] of Object.entries(mixin.computed)) {
    Object.defineProperty(model, name, { get: () => getter.call(model) });
  }
  model.llmPresets = { saved: { model: 'old', base_url: 'https://fixture.test' } };
  model.selectPreset('saved');
  return { model, sandbox, toasts };
}

const response = (status, data) => ({ status, ok: status < 400, json: async () => data });

test('connection testing blocks an edited model or key before making a saved-preset request', async () => {
  const calls = [];
  const { model } = settings(async (...args) => {
    calls.push(args);
    return response(200, { ok: true, latency_ms: 1 });
  });
  model.presetForm.model = 'draft';
  await model.testLLMPreset();
  assert.equal(calls.length, 0);
  assert.equal(model.llmPresetTestDirty, true);
  assert.equal(model.llmModelConnectionDirty, false, 'model discovery still works with an edited model');
  model.presetForm.model = 'old';
  model.presetForm.api_key = 'synthetic-key';
  await model.testLLMPreset();
  assert.equal(calls.length, 0);
  model.presetForm.api_key = '';
  await model.testLLMPreset();
  assert.equal(calls.length, 1);
  assert.equal(model.llmTestResult.ok, true);
});

test('clearing advanced request JSON sends an explicit null without changing the token limit', async () => {
  let payload;
  const { model } = settings(async (_url, options) => {
    payload = JSON.parse(options.body);
    return response(200, { ok: true });
  });
  model.loadLLMPresets = async () => true;
  model.loadSetupGuide = async () => {};
  model.presetForm.extra_body_raw = '';
  model.presetForm.max_tokens_enabled = true;
  model.presetForm.max_tokens = 777;
  await model.saveLLMPreset();
  assert.equal(payload.extra_body, null);
  assert.equal(payload.max_tokens, 777);
});

test('connection testing shows the HTTP error detail', async () => {
  const { model } = settings(async () => response(404, { detail: 'Preset missing' }));
  await model.testLLMPreset();
  assert.equal(model.llmTestResult.ok, false);
  assert.equal(model.llmTestResult.error, 'Preset missing');
});

test('failed model deletion leaves selection and draft in place', async () => {
  const { model, toasts } = settings(async () => response(409, { detail: 'Preset in use' }));
  model.presetForm.model = 'draft';
  await model._doDeleteLLMPreset('saved');
  assert.equal(model.selectedPresetName, 'saved');
  assert.equal(model.presetForm.model, 'draft');
  assert.match(toasts[0][0], /Preset in use/);
});

test('missing model references distinguish a removed preset from sentinels and inherited object properties', () => {
  const { model } = settings();
  for (const value of [null, '', '__none__', '__active__', 'saved']) {
    assert.equal(model.isMissingModelPreset(value), false, String(value));
  }
  for (const value of ['removed', 'toString']) assert.equal(model.isMissingModelPreset(value), true);
});

test('deletion confirmation names explicit dependencies, including an unloaded persona', async () => {
  const { model } = settings();
  model.agentSettingsForm.model_preset = 'saved';
  model.analysisModelPreset = 'saved';
  model.reflectionForm.reflection_primary_preset = '__active__';
  model.reflectionForm.reflection_fallback_preset = 'saved';
  model.reflectionForm.vlm_model_preset = 'saved';
  model.profiles = [
    { profile_id: 'one', display_name: 'Fixture', load_into_chat: false,
      memory_config: { extraction_llm_preset: 'saved' } },
    { profile_id: 'two', display_name: 'Other', memory_config: { extraction_llm_preset: '' } },
  ];
  await model.deleteLLMPreset();
  const message = model.confirmingAction.label;
  for (const label of ['agentModel', 'llmAnalysisSectionTitle', 'lblFallbackModel', 'lblVlmModel', 'Fixture']) {
    assert.match(message, new RegExp(label));
  }
  assert.doesNotMatch(message, /lblPrimaryModel|Other/);
});

test('Voice save keeps confirmed success if reconnect throws', async () => {
  const { model, toasts } = settings(async () => response(200, { ok: true }));
  model.ttsEnabled = true;
  model.disconnectTTS = () => { throw new Error('socket cleanup failed'); };
  await model.saveTTSConfig();
  assert.equal(model.ttsSaveState, 'saved');
  assert.deepEqual(toasts, [['toastTtsSaved', 'success']]);
});

test('Voice autosave reports the failed write with the server detail', async () => {
  const { model, toasts } = settings(async () => response(422, { detail: 'Voice rejected' }));
  await model.saveTTSConfig(true);
  assert.equal(model.ttsSaveState, 'error');
  assert.match(toasts[0][0], /Voice rejected/);
});

test('Voice preview does not play a failed HTTP response', async () => {
  const { model, sandbox } = settings(async () => response(502, { ok: true, mime: 'audio/wav', audio: 'fixture' }));
  let played = false;
  sandbox.Audio = class { async play() { played = true; } };
  await model.testTTSVoice();
  assert.equal(played, false);
  assert.equal(model.ttsTestState, 'error');
});

test('tool model save skips runtime probing and keeps the numeric Tools drafts across tab loads', async () => {
  const { model } = settings();
  const requests = [];
  let saved = { max_model_requests: 8, timeout_seconds: 90, autonomy_interval_seconds: 900 };
  model.agentFetch = async (url, options) => {
    requests.push(url);
    if (options?.method === 'PATCH') saved = { ...saved, ...JSON.parse(options.body) };
    return saved;
  };
  await model.loadAgentSettings(false, false);
  model.agentSettingsForm.max_model_requests = 11;
  model.agentSettingsForm.autonomy_interval_seconds = 1000;
  model.activeTab = 'llm';
  await model.saveAgentSettings({ model_preset: 'saved' });
  await model.loadAgentSettings(false, false);
  assert.deepEqual(requests, Array(3).fill('/api/agent/settings'));
  assert.equal(model.agentSettingsForm.model_preset, 'saved');
  assert.equal(model.agentSettingsForm.max_model_requests, 11);
  assert.equal(model.agentSettingsForm.autonomy_interval_seconds, 1000);
  await model.saveAgentSettings({ max_model_requests: 11 });
  saved.max_model_requests = 12;
  await model.loadAgentSettings(false, false);
  assert.equal(model.agentSettingsForm.max_model_requests, 12, 'a saved limit follows future reads');
});

test('runtime status failure stays separate from successful Tools save and its drafts', async () => {
  const { model, toasts } = settings();
  model.activeTab = 'tools';
  model.agentFetch = async (url, options) => {
    if (url.endsWith('/status')) throw new Error('status unavailable');
    return { enabled: !!options, timeout_seconds: 90 };
  };
  await model.loadAgentSettings(false, false);
  model.agentSettingsForm.timeout_seconds = 120;
  await model.saveAgentSettings({ enabled: true });
  assert.equal(model.agentSettingsForm.enabled, true);
  assert.equal(model.agentSettingsForm.timeout_seconds, 120);
  assert.equal(model.agentSettingsError, '');
  assert.equal(model.agentStatusError, 'status unavailable');
  assert.deepEqual(toasts, [['savedOk', 'success']]);
});

test('disabling the global workspace clears the global code default in the same patch', async () => {
  const { model } = settings();
  const profile = { profile_id: 'fixture', tool_configs: { agent: { workspace_enabled: true, code_enabled: true } } };
  model.profiles = [profile];
  let sent;
  model.agentFetch = async (_url, options) => {
    if (options?.method === 'PATCH') {
      sent = JSON.parse(options.body);
      return sent;
    }
    return { workspace_enabled: true, code_enabled: true };
  };
  await model.loadAgentSettings(false, false);
  await model.saveAgentSettings({ workspace_enabled: false });
  assert.deepEqual(sent, { workspace_enabled: false, code_enabled: false });
  assert.equal(model.agentSettingsForm.code_enabled, false);
  assert.deepEqual(profile.tool_configs.agent, { workspace_enabled: true, code_enabled: true });
});

test('retrying runtime status does not reload settings or overwrite unsaved edits', async () => {
  const { model } = settings();
  const requests = [];
  model.agentFetch = async (url, options) => {
    requests.push([url, options?.method]);
    return { state: 'ready' };
  };
  model.agentSettingsForm.timeout_seconds = 111;
  await model.loadAgentStatus(true);
  assert.deepEqual(requests, [['/api/agent/check', 'POST']]);
  assert.equal(model.agentSettingsForm.timeout_seconds, 111);
  assert.equal(model.agentRuntime.state, 'ready');
});

test('opening Tools while Models is loading still fetches runtime status', async () => {
  const { model } = settings();
  let finishRead;
  const requests = [];
  model.agentFetch = async url => {
    requests.push(url);
    if (url.endsWith('/settings')) return new Promise(resolve => { finishRead = resolve; });
    return { state: 'ready' };
  };
  const readingModels = model.loadAgentSettings(false, false);
  await model.loadAgentSettings();
  finishRead({ model_preset: 'saved' });
  await readingModels;
  assert.deepEqual(requests, ['/api/agent/settings', '/api/agent/status']);
  assert.equal(model.agentRuntime.state, 'ready');
});

test('web research uses reflection settings and drops retired source fields', async () => {
  const requests = [];
  const { model } = settings(async (url, options) => {
    requests.push({ url, options });
    if (options?.method === 'POST') return response(200, { ok: true });
    return response(200, { topic_discovery: { sources: {
      trend: { enabled: true },
      user_life: { enabled: false },
      random_api: { enabled: true, mode: 'http', http_url: 'https://old.invalid', builtin_pool: 'trivia' },
    } } });
  });
  await model.loadReflectionConfig();
  assert.equal(model.reflectionForm.topic_discovery.sources.web_research.enabled, false);
  model.reflectionForm.topic_discovery.sources.web_research.enabled = true;
  await model.saveReflectionConfig();
  assert.deepEqual(requests.map(({ url }) => url), Array(2).fill('http://fixture.test/settings/reflection'));
  const { topic_discovery } = JSON.parse(requests[1].options.body);
  assert.deepEqual(topic_discovery.sources.web_research, { enabled: true });
  assert.deepEqual(topic_discovery.sources.random_api, { enabled: true, builtin_pool: 'trivia' });
  assert.equal(topic_discovery.sources.trend, undefined);
  assert.equal(topic_discovery.sources.user_life, undefined);
});
