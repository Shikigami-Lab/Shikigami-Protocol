const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { join } = require('node:path');
const { runInNewContext } = require('node:vm');

const root = join(__dirname, '../static');
function icons() {
  const context = { window: {} };
  runInNewContext(fs.readFileSync(join(root, 'js/icons.js'), 'utf8'), context);
  return context.window.SK_ICONS;
}

test('Lucide toolbar renders local SVG with consistent decorative semantics', () => {
  const { render } = icons();
  const svg = render('image-plus', 16);
  assert.match(svg, /^<svg /);
  assert.match(svg, /width="16" height="16"/);
  assert.match(svg, /aria-hidden="true"/);
  assert.match(svg, /stroke="currentColor"/);
  assert.match(svg, /<path /);
  assert.equal(render('toString'), '');
  assert.equal(render('<img onerror=alert(1)>'), '');
  assert.match(render('timer', '" onload="alert(1)'), /width="18"/);
});

test('standalone placeholders and Vue use the same icon resource', () => {
  const { hydrate, component } = icons();
  const node = { dataset: { icon: 'sliders-horizontal', iconSize: '14' } };
  hydrate({ querySelectorAll: selector => selector === '[data-icon]' ? [node] : [] });
  assert.match(node.innerHTML, /width="14".*<path /s);
  assert.match(component.template, /v-html="paths"/);
  const html = fs.readFileSync(join(root, 'index.html'), 'utf8');
  assert.match(html, /js\/icons\.js.*[\s\S]*js\/app\.js/);
});

test('declared static and conditional icon names are included in the offline resource', () => {
  const { render } = icons();
  const sources = ['index.html', 'setup_wizard.html', 'docs-viewer.html', 'splash.html',
    'js/components.js', 'js/app.js', 'js/settings.js', 'js/i18n.js', 'js/setup_wizard.js'];
  let checked = 0;
  for (const file of sources) {
    const source = fs.readFileSync(join(root, file), 'utf8');
    const names = new Set();
    for (const [tag] of source.matchAll(/<app-icon\b(?:[^>"']|"[^"]*"|'[^']*')*>/g)) {
      const attribute = tag.match(/\s(:name|name)\s*=\s*(["'])(.*?)\2/s);
      if (!attribute) continue;
      if (attribute[1] === 'name' && /^[a-z0-9-]+$/.test(attribute[3])) names.add(attribute[3]);
      if (attribute[1] === ':name') {
        const expression = attribute[3];
        for (const match of expression.matchAll(/['"]([a-z0-9-]+)['"]/g)) {
          const prefix = expression.slice(0, match.index).trimEnd();
          const suffix = expression.slice(match.index + match[0].length).trimStart();
          if (!/[=!]={1,2}$/.test(prefix) && !/^[=!]={1,2}/.test(suffix)) names.add(match[1]);
        }
      }
    }
    for (const [,name] of source.matchAll(/(?:\bdata-icon\s*=\s*|\bicon\s*:\s*|\bSK_ICONS\.render\(\s*)['"]([a-z0-9-]+)['"]/g)) {
      names.add(name);
    }
    for (const name of names) {
      assert.ok(render(name), `${file} references an unavailable local icon: ${name}`);
      checked++;
    }
  }
  assert.ok(checked >= 30, 'Icon coverage should include multiple application surfaces');
});
