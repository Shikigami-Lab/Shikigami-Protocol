const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');

const root = join(__dirname, '..');
const html = readFileSync(join(root, 'static', 'index.html'), 'utf8');
const css = readFileSync(join(root, 'static', 'css', 'settings.css'), 'utf8');
const picker = html.split('<div class="theme-picker">')[1].split('<!--')[0];

test('every selectable theme previews the same palette tokens as settings', () => {
  const cards = [...picker.matchAll(/data-theme-palette="([^"]+)"/g)].map(match => match[1]);
  const themes = [...picker.matchAll(/applyTheme\('([^']*)'\)/g)].map(match => match[1] || 'purple');
  assert.equal(themes.length, 11);
  assert.deepEqual(cards, themes);
  assert.equal(new Set(cards).size, 11);
  assert.doesNotMatch(picker, /class="theme-swatch-(?:bar|body)"\s+style=/);
  for (const theme of cards) {
    const selector = css.indexOf(`[data-theme-palette="${theme}"]`);
    assert.ok(selector >= 0, `Missing palette for ${theme}`);
    const brace = css.indexOf('{', selector);
    const block = css.slice(brace, css.indexOf('}', brace));
    for (const role of ['page', 'card', 'nav', 'border', 'primary', 'secondary', 'accent']) {
      assert.match(block, new RegExp(`--settings-palette-${role}: #[a-f0-9]{6}`, 'i'), `${theme} needs ${role}`);
    }
  }
  assert.match(css, /\.theme-swatch-bar\s*\{[^}]*var\(--settings-palette-accent\)/);
  assert.match(css, /\.theme-swatch-body\s*\{[^}]*var\(--settings-palette-card\)/);
});

test('the settings drawer consumes palette roles through its scoped theme variables', () => {
  const block = css.match(/(?:^|\n)\.settings-drawer\s*\{([^}]+)\}/)[1];
  for (const [token, role] of Object.entries({
    'bg-panel': 'page', 'bg-app': 'card', 'bg-sidebar': 'nav',
    border: 'border', 'text-primary': 'primary', 'text-secondary': 'secondary', accent: 'accent',
  })) {
    assert.match(block, new RegExp(`--${token}: var\\(--settings-palette-${role}\\)`));
  }
});
