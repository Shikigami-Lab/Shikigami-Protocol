'use strict';

// Test-only entry point. Production main.js creates every window and spawns
// server.py normally; this wrapper observes it and drives the rendered DOM.
const fs = require('node:fs');
const path = require('node:path');
const childProcess = require('node:child_process');
const { app, BrowserWindow, session } = require('electron');

const runtime = process.env.SHIKIGAMI_SMOKE_RUNTIME;
const output = process.env.SHIKIGAMI_SMOKE_OUTPUT;
const port = Number(process.env.SHIKIGAMI_SMOKE_PORT);
const profile = process.env.SHIKIGAMI_SMOKE_PROFILE;
const reply = process.env.SHIKIGAMI_SMOKE_REPLY;
if (!runtime || !output || !profile || !reply || !port || [7788, 8000].includes(port)) {
  throw new Error('Isolated synthetic smoke environment is required');
}
const origin = `http://127.0.0.1:${port}`;
const write = (name, data) => fs.writeFileSync(path.join(output, name), JSON.stringify(data, null, 2));
const report = { ok: false, platform: process.platform, arch: process.arch,
  electron: process.versions.electron, node: process.versions.node, checks: [], errors: [],
  excluded_optional_routes: ['/api/setup/status', '/settings/tts/voices'] };

// main.js takes the single-instance lock at module load, so isolation must
// precede require(main.js), not merely precede app.whenReady().
const userData = path.join(output, 'userData');
fs.mkdirSync(userData, { recursive: true });
app.setPath('userData', userData);
app.setPath('sessionData', userData);
app.setAppLogsPath(path.join(output, 'logs'));

let backend;
const originalSpawn = childProcess.spawn;
childProcess.spawn = function (command, args, options) {
  const child = originalSpawn.apply(this, arguments);
  if (options?.cwd === runtime && args?.[0] === path.join(runtime, 'server.py')) {
    backend = child;
    report.backend_pid = child.pid;
    write('backend.json', { pid: child.pid, command, script: args[0] });
  }
  return child;
};

const hideCallbacks = new WeakMap();
app.on('browser-window-created', (_event, win) => {
  win.setSkipTaskbar(true);
  win.webContents.setBackgroundThrottling(false);
  const hide = () => win.hide();
  hideCallbacks.set(win, hide);
  win.on('show', hide);
  win.webContents.on('console-message', (_event, level, message) => {
    console.log(`[renderer:${level}] ${message}`);
  });
  win.webContents.on('render-process-gone', (_event, details) => {
    report.errors.push(`renderer process gone: ${details.reason}`);
  });
});

let finished = false;
function finish(error) {
  if (finished) return;
  finished = true;
  clearTimeout(watchdog);
  if (error) report.error = String(error.stack || error);
  report.ok = !error && report.errors.length === 0;
  write('renderer.json', report);
  process.exitCode = report.ok ? 0 : 1;
  // app.quit emits production main.js's before-quit and kills its Python child.
  app.quit();
}
const watchdog = setTimeout(() => finish(new Error('Desktop smoke timed out')), 90000);
process.on('uncaughtException', finish);
process.on('unhandledRejection', finish);

// Load exactly the committed source entry point, with its normal __dirname.
require(path.join(runtime, 'main.js'));

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(description, probe) {
  const deadline = Date.now() + 35000;
  while (Date.now() < deadline) {
    const result = await probe();
    if (result) return result;
    await delay(100);
  }
  throw new Error(`Timed out waiting for ${description}`);
}

async function smoke() {
  await app.whenReady();
  // Chromium traffic must remain local too (including background services).
  session.defaultSession.webRequest.onBeforeRequest((details, callback) => {
    const url = new URL(details.url);
    // The main UI eagerly requests GPU onboarding status and online Edge TTS
    // voice catalogs. Neither is part of source-desktop acceptance. Cancel
    // those optional requests, rather than probing host hardware or the web.
    if (url.origin === origin && report.excluded_optional_routes.includes(url.pathname)) {
      callback({ cancel: true });
      return;
    }
    const allowed = ['file:', 'data:', 'devtools:', 'chrome:'].includes(url.protocol)
      || (['http:', 'ws:'].includes(url.protocol) && url.hostname === '127.0.0.1' && Number(url.port) === port);
    if (!allowed) report.errors.push(`forbidden renderer request: ${url.origin}`);
    callback({ cancel: !allowed });
  });
  const win = await until('real main window', () => BrowserWindow.getAllWindows().find(
    candidate => !candidate.isDestroyed() && candidate.webContents.getURL() === origin + '/'));
  const evaluate = code => win.webContents.executeJavaScript(code, true);
  await until('Vue mount and synthetic character', () => evaluate(`(() => {
    const root = document.querySelector('[data-v-app]');
    const character = document.querySelector('[data-session-id=' + ${JSON.stringify(profile)} + ']');
    const input = document.querySelector('.input-row textarea');
    return !!(root && character && input && input.getBoundingClientRect().width > 0
      && !root.textContent.includes('{{'));
  })()`));
  if (!backend?.pid || backend.exitCode !== null) throw new Error('No live backend spawned by real main.js');
  report.url = win.webContents.getURL();
  report.checks.push('real-main-spawned-backend', 'vue-rendered');
  console.log('[smoke] Vue rendered; opening Settings');

  await evaluate(`document.querySelector('.gear-btn').click()`);
  await until('Settings dialog and content', () => evaluate(`(() => {
    const drawer = document.querySelector('.settings-drawer.open');
    return !!(drawer && drawer.querySelector('[role=dialog][aria-modal=true]')
      && drawer.querySelector('.settings-tab-content') && drawer.getBoundingClientRect().width > 0);
  })()`));
  await delay(400); // allow the drawer transition to finish before capture
  async function capture(name) {
    console.log(`[smoke] capturing ${name}`);
    // Windows can leave a hidden compositor without a captureable surface.
    // Show only the synthetic window, without taking focus, during capture.
    const hide = hideCallbacks.get(win);
    if (hide) win.removeListener('show', hide);
    let timer;
    try {
      win.showInactive();
      await delay(300);
      const image = await Promise.race([
        win.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true }),
        new Promise((_resolve, reject) => { timer = setTimeout(() => reject(new Error('Screenshot timed out')), 10000); }),
      ]);
      if (image.isEmpty()) throw new Error('Empty rendered screenshot');
      fs.writeFileSync(path.join(output, name), image.toPNG());
    } finally {
      clearTimeout(timer);
      win.hide();
      if (hide) win.on('show', hide);
    }
  }
  await capture('settings.png');
  report.checks.push('settings-opened');
  console.log('[smoke] Settings opened; sending synthetic chat');
  await evaluate(`document.querySelector('.settings-drawer.open .canvas-close-btn').click()`);
  await until('Settings closed', () => evaluate(`!document.querySelector('.settings-drawer.open')`));
  await evaluate(`(() => {
    const input = document.querySelector('.input-row textarea');
    input.value = 'Synthetic desktop test request';
    input.dispatchEvent(new Event('input', { bubbles: true }));
  })()`);
  await until('enabled send button', () => evaluate(`!document.querySelector('.send-btn').disabled`));
  await evaluate(`document.querySelector('.send-btn').click()`);
  await until('synthetic reply in rendered assistant bubble', () => evaluate(`
    [...document.querySelectorAll('.message-row.assistant .bubble-content')]
      .some(node => node.textContent.trim() === ${JSON.stringify(reply)})
      && !document.querySelector('.input-row textarea').disabled`));
  await capture('chat.png');
  report.checks.push('chat-reply-rendered');
}

smoke().then(() => finish(), finish);
