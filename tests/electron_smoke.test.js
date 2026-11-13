'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const wrapper = fs.readFileSync(path.join(__dirname, '../.github/scripts/electron_smoke.cjs'), 'utf8');

async function driveWrapper({ emptyCapture = false } = {}) {
  const runtime = path.resolve('/synthetic/source');
  const output = path.resolve('/synthetic/evidence');
  const events = [];
  const writes = new Map();
  const captures = [];
  let quit;
  const done = new Promise(resolve => { quit = resolve; });
  const fakeProcess = { env: { SHIKIGAMI_SMOKE_RUNTIME: runtime, SHIKIGAMI_SMOKE_OUTPUT: output,
    SHIKIGAMI_SMOKE_PORT: '54321', SHIKIGAMI_SMOKE_PROFILE: 'synthetic', SHIKIGAMI_SMOKE_REPLY: 'fixture reply' },
    platform: 'win32', arch: 'x64', versions: { electron: '31.fixture', node: '20.fixture' }, on() {} };
  const webContents = {
    getURL: () => 'http://127.0.0.1:54321/',
    executeJavaScript: async () => true,
    capturePage: async (rect, options) => {
      captures.push({ rect, options });
      return { isEmpty: () => emptyCapture, toPNG: () => Buffer.from('synthetic PNG') };
    },
  };
  const app = { setPath(name) { events.push(name); }, setAppLogsPath() {}, on() {},
    whenReady: async () => {}, quit() { quit(); } };
  const childProcess = { spawn: () => ({ pid: 12345, exitCode: null }) };
  const context = {
    process: fakeProcess, console: { log() {} }, URL, setTimeout, clearTimeout,
    require(name) {
      if (name === 'electron') return { app,
        BrowserWindow: { getAllWindows: () => [{ isDestroyed: () => false, webContents,
          showInactive() {}, hide() {} }] },
        session: { defaultSession: { webRequest: { onBeforeRequest() {} } } } };
      if (name === 'node:fs') return { mkdirSync() {}, writeFileSync(file, data) { writes.set(path.basename(file), data); } };
      if (name === 'node:path') return path;
      if (name === 'node:child_process') return childProcess;
      if (name === path.join(runtime, 'main.js')) {
        events.push('main.js');
        childProcess.spawn('python', [path.join(runtime, 'server.py')], { cwd: runtime });
        return {};
      }
      throw new Error('Unexpected module: ' + name);
    },
  };
  vm.runInNewContext(wrapper, context, { filename: 'electron_smoke.cjs' });
  await done;
  return { events, captures, report: JSON.parse(writes.get('renderer.json')), code: fakeProcess.exitCode };
}

test('desktop wrapper isolates the lock before loading real main and captures while hidden', async () => {
  const result = await driveWrapper();
  assert.ok(result.events.indexOf('userData') < result.events.indexOf('main.js'));
  assert.equal(result.captures.length, 2);
  for (const capture of result.captures) assert.equal(capture.options?.stayHidden, true);
  assert.equal(result.report.ok, true);
  assert.equal(result.report.backend_pid, 12345);
  assert.equal(result.code, 0);
});

test('desktop wrapper rejects an empty screenshot and still quits', async () => {
  const result = await driveWrapper({ emptyCapture: true });
  assert.equal(result.report.ok, false);
  assert.match(result.report.error, /Empty rendered screenshot/);
  assert.equal(result.code, 1);
});
