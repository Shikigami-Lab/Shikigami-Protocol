'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'start.js'), 'utf8');

function launch({ platform = 'win32', missing = false } = {}) {
  const child = new EventEmitter();
  const calls = [];
  const errors = [];
  const exits = [];
  const binary = platform === 'win32'
    ? 'C:\\Electron Test !\\electron.exe' : '/Electron Test !/electron';
  const process = { platform, env: { KEEP: 'yes' }, exit: code => exits.push(code) };
  vm.runInNewContext(source, {
    __dirname: root, process,
    console: { error: (...args) => errors.push(args.join(' ')) },
    require(name) {
      if (name === 'path') return path;
      if (name === 'electron') {
        if (missing) throw new Error('Cannot find module electron');
        return binary;
      }
      if (name === 'child_process') return {
        spawn(executable, args, options) {
          calls.push({ executable, args: Array.from(args), options });
          return child;
        },
      };
      throw new Error('Unexpected require: ' + name);
    },
  }, { filename: 'start.js' });
  return { child, calls, errors, exits, binary };
}

for (const platform of ['win32', 'darwin', 'linux']) {
  test(`dev launcher spawns Electron binary directly on ${platform}`, () => {
    const h = launch({ platform });
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].executable, h.binary);
    assert.deepEqual(h.calls[0].args, ['.']);
    assert.equal(h.calls[0].options.cwd, root);
    assert.equal(h.calls[0].options.env.NODE_ENV, 'development');
    assert.equal(h.calls[0].options.env.KEEP, 'yes');
    assert.equal(h.calls[0].options.stdio, 'inherit');
    assert.ok(!h.calls[0].options.shell);
    h.child.emit('exit', 7);
    assert.deepEqual(h.exits, [7]);
  });
}

test('dev launcher reports a missing install without spawning', () => {
  const h = launch({ missing: true });
  assert.equal(h.calls.length, 0);
  assert.deepEqual(h.exits, [1]);
  assert.match(h.errors.join(' '), /npm install/);
});

test('dev launcher reports spawn failure without an unhandled error', () => {
  const h = launch();
  h.child.emit('error', new Error('ENOENT fixture'));
  assert.deepEqual(h.exits, [1]);
  assert.match(h.errors.join(' '), /ENOENT fixture/);
});
