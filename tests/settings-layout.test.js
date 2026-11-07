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
  const memory = html.slice(html.indexOf(`v-if="activeTab === 'memory' || isMemoryBrowser"`));
  assert.ok(!models.includes('v-model="selectedProfileId"'));
  assert.ok(!models.includes('v-model="memoryTabProfileForm.extraction_llm_preset"'));
  const preset = memory.match(/<select id="memory-extraction-model"[\s\S]*?<\/select>/)?.[0] || '';
  assert.ok(preset.includes('v-model="memoryTabProfileForm.extraction_llm_preset"'));
  assert.ok(preset.includes('<option :value="null">{{ t(\'optInheritGlobal\') }}</option>'));
  assert.ok(preset.includes('<option value="">{{ t(\'optActiveModel\') }}</option>'));
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


test('memory browser and settings use distinct views and maintenance has a collapsed cleanup section', () => {
  const browser = html.indexOf('class="memory-panel-content"');
  const settings = html.indexOf('class="memory-panel-settings"');
  assert.ok(browser > 0 && settings > browser);
  assert.match(html.slice(browser - 65, browser), /v-if="isMemoryBrowser"/);
  assert.match(html.slice(settings - 65, settings), /v-else/);
  const maintenance = html.slice(html.indexOf("memoryActiveSubTab === 'maintenance'"), html.indexOf("memoryActiveSubTab === 'vectors'"));
  assert.match(maintenance, /<details class="[^"]*memory-cleanup/);
  assert.match(maintenance, /<button[^>]+class="s-btn s-btn-secondary"[^>]+@click="loadForgettingPreview/);
});

test('manual memory curation exposes its preview and apply separately from local maintenance', () => {
  const memory = html.slice(html.indexOf('class="memory-panel-content"'), html.indexOf('class="memory-panel-settings"'));
  const window = {};
  runInNewContext(readFileSync(join(__dirname, '../static/js/api-paths.js'), 'utf8'), { window });
  const { API_PATHS } = window;
  assert.equal(API_PATHS.memoryCurationPreview('a/b'), '/memory/a%2Fb/curation/preview');
  assert.equal(API_PATHS.memoryCurationApply('a/b'), '/memory/a%2Fb/curation/apply');
  assert.equal(API_PATHS.memoryFactRestore('a/b', 'f/1'), '/memory/a%2Fb/facts/f%2F1/restore');
  for (const binding of [
    'factFilterStatus', 'fact.archive_note', 'fact.review_note', 'memoryArchiveRecallLabel(fact)',
    'restoreMemoryFact(fact)', 'loadForgettingPreview(memoryProfileId)', 'loadMemoryCurationPreview(memoryProfileId)',
    'hintMemoryCurationModel', 'applyMemoryCuration()', 'item.content', 'item.reason', 'item.target_content',
    'item.archive_reason', 'memoryCurationCounts', 'memoryCurationResult',
    'v-model="memoryCurationSelection"', ':value="item.fact_id"', 'btnMemoryCurationMarkReviewed',
  ]) assert.ok(memory.includes(binding), binding);
});

test('old-store preview labels the per-page cost and exposes read-only controls in Maintenance', () => {
  const window = {};
  for (const filename of ['api-paths.js', 'i18n.js']) {
    runInNewContext(readFileSync(join(__dirname, '../static/js', filename), 'utf8'), { window });
  }
  assert.equal(window.API_PATHS.memoryCurationBulkPreview('a/b'), '/memory/a%2Fb/curation/bulk-preview');
  const bulk = html.match(/<details id="memory-bulk-curation"[\s\S]*?<\/details>/)?.[0] || '';
  assert.match(bulk, /id="memory-bulk-curation-hint"/);
  assert.match(bulk, /aria-describedby="memory-bulk-curation-hint"/);
  assert.match(bulk, /@click="loadMemoryBulkCurationPage\(memoryProfileId\)"/);
  assert.match(bulk, /memoryBulkCurationPage.next_cursor !== null/);
  assert.match(bulk, /memoryBulkCurationPage.items/);
  assert.match(bulk, /id="memory-bulk-curation-coverage"/);
  assert.doesNotMatch(bulk, /applyMemoryCuration|type="checkbox"|@toggle/);
  assert.match(window.LOCALES.en.memBulkCurationTitle, /read-only/i);
  assert.match(window.LOCALES.en.hintMemoryBulkCuration, /each page.*one model request.*charges/i);
  assert.match(window.LOCALES.zh.memBulkCurationTitle, /只读/);
  assert.match(window.LOCALES.zh.hintMemoryBulkCuration, /每页.*一次模型请求.*费用/);
});

test('daily curation controls are independently addressable and Maintenance renders durable and session results', () => {
  const toggle = html.match(/<input id="memory-daily-curation-global"[^>]*>/)?.[0] || '';
  assert.match(toggle, /v-model="memoryGlobalForm.daily_curation_enabled"/);
  assert.match(toggle, /:disabled="!memoryGlobalFormLoaded"/);
  const select = html.match(/<select id="memory-daily-curation-profile"[\s\S]*?<\/select>/)?.[0];
  assert.match(select || '', /v-model="memoryTabProfileForm.daily_curation_enabled"/);
  for (const value of ['null', 'true', 'false']) assert.ok(select.includes(':value="' + value + '"'));
  const maintenance = html.slice(html.indexOf("memoryActiveSubTab === 'maintenance'"), html.indexOf("memoryActiveSubTab === 'vectors'"));
  assert.match(maintenance, /id="memory-curation-latest"/);
  assert.match(maintenance, /id="memory-curation-daily-status"/);
  assert.match(maintenance, /id="memory-curation-index-pending"\s+v-if="memoryStatus.vector_ok && memoryStatus.curation.index_pending"/);
  assert.match(maintenance, /v-if="memoryCurationResult"/);
  for (const key of ['memoryStatus.curation.latest.origin', 'memoryStatus.curation.latest.at',
    'memoryStatus.curation.latest.counts.merged', 'memoryStatus.curation.latest.counts.archived',
    'memoryStatus.curation.latest.counts.reviewed', 'memoryStatus.curation.daily.day', 'memoryStatus.curation.daily.status']) {
    assert.ok(maintenance.includes(key), key);
  }
  const window = {};
  runInNewContext(readFileSync(join(__dirname, '../static/js/i18n.js'), 'utf8'), { window });
  for (const locale of ['en', 'zh']) {
    const keys = ['hintDailyCuration', 'memCurationOrigin_manual', 'memCurationOrigin_daily', 'memCurationIndexPending',
      ...['running', 'completed', 'failed', 'cancelled', 'stale'].map(status => 'memDailyCurationStatus_' + status)];
    for (const key of keys) assert.ok(window.LOCALES[locale][key], locale + ' ' + key);
  }
});

test('the Facts view exposes experiences, original evidence and text changes using localized controls', () => {
  const memory = html.slice(html.indexOf('class="memory-panel-content"'), html.indexOf('class="memory-panel-settings"'));
  const window = {};
  for (const filename of ['api-paths.js', 'i18n.js']) {
    runInNewContext(readFileSync(join(__dirname, '../static/js', filename), 'utf8'), { window });
  }
  assert.equal(window.API_PATHS.memoryFactEvidence('a/b', 'f/1'), '/memory/a%2Fb/facts/f%2F1/evidence');
  assert.match(memory, /<option value="experiences">/);
  assert.match(memory, /:data-fact-id="fact.id"/);
  assert.match(memory, /@toggle="\$event.target.open && loadMemoryFactEvidence\(fact\)"/);
  for (const binding of [
    'fact.kind', 'fact.occurred_from', 'fact.occurred_to', 'fact.retention_reason',
    'item.available', 'item.content', 'item.source', 'item.role', 'item.timestamp', 'item.id',
    'item.start_char', 'item.end_char', 'item.truncated', 'memoryFactVersionComparisons(fact)',
  ]) assert.ok(memory.includes(binding), binding);
  assert.doesNotMatch(memory, /v-html="item.content"/);
  for (const locale of ['en', 'zh']) {
    for (const key of ['memFactFilterExperiences', 'memFactKind_experience', 'memFactOccurredFrom',
      'memFactOccurredTo', 'memFactRetentionReason', 'memFactEvidence', 'memFactEvidenceUnavailable', 'memFactEvidenceTruncated',
      'memFactVersionBefore', 'memFactVersionAfter']) {
      assert.ok(window.LOCALES[locale][key], locale + ' ' + key);
    }
  }
});
