const { readFileSync } = require('node:fs');
const assert = require('node:assert/strict');
const { join } = require('node:path');
const { test } = require('node:test');

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
