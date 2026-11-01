import test from 'node:test';
import assert from 'node:assert/strict';
import { installCharacterPrompt } from '../prompt-context.mjs';

test('working directory belongs in the literal character system prompt', () => {
  const sections = [], contexts = [], listeners = [];
  let suppressed = false;
  const ctx = {
    systemPrompt: {
      section: value => sections.push(value),
      context: value => contexts.push(value),
      getContextOrder: name => { assert.equal(name, 'WORKING_DIRECTORY'); return 0; },
      suppressRuntimeContext: () => { suppressed = true; },
    },
    on: (...args) => listeners.push(args),
  };
  const original = 'Character {{literal}}\nSnapshot is also an ordinary user word.';
  installCharacterPrompt(ctx, original);
  assert.equal(sections.length, 1);
  assert.equal(sections[0].complete, true);
  assert.equal(sections[0].interpolate, false);
  assert.ok(sections[0].text.startsWith(original));
  assert.match(sections[0].text, /\[Execution environment\][\s\S]*\/workspace/);
  assert.equal(contexts[0].name, 'working-directory:current');
  assert.equal(contexts[0].text, ''); // Admission refresh must not resurrect it.
  assert.equal(contexts[0].required, true);
  assert.equal(suppressed, true);
  assert.equal(listeners[0][0], 'system-prompt/assemble');
  assert.equal(listeners[0][2].prepend, true);
});

test('assembly removes only the SDK directory contribution, never history or tools', async () => {
  let listener;
  const ctx = { systemPrompt: { section() {}, context() {}, getContextOrder() { return 0; }, suppressRuntimeContext() {} },
    on: (_event, fn) => { listener = fn; } };
  installCharacterPrompt(ctx, 'Persona');
  const preserved = { name: 'other-required-context', text: 'Another extension' };
  const assembly = { sections: [{ text: 'Persona' }], contexts: [
    { name: 'working-directory:current', text: 'Current working directory: "/workspace".' }, preserved],
    tools: [{ name: 'read' }], variables: { value: '{{literal}}' } };
  const result = await listener({}, {}, async () => assembly);
  assert.deepEqual(result.contexts, [preserved]);
  assert.equal(result.tools, assembly.tools);
  assert.equal(result.sections, assembly.sections);
  assert.equal(result.variables, assembly.variables);
  assert.equal(assembly.contexts.length, 2, 'do not mutate other listeners input');
});
