'use strict';

/**
 * main.js — Shikigami Protocol Electron main process
 *
 * Responsibilities:
 *   1. Read host/port from config/app.yaml (never hardcode)
 *   2. Kill any stale process holding that port, then spawn server.py
 *   3. Poll /sessions until server is ready, then create BrowserWindow
 *   4. System tray: close window → hide (not quit)
 *   5. IPC: 'take-screenshot' → desktopCapturer → base64 PNG
 *   6. IPC: 'quit-app' → kill server → app.quit()
 */

const {
  app,
  BrowserWindow,
  Tray,
  Menu,
  ipcMain,
  desktopCapturer,
  nativeImage,
  screen,
  shell,
  dialog,
} = require('electron');
const { spawn, exec } = require('child_process');
const path = require('path');
const http = require('http');
const fs = require('fs');
const { autoUpdater } = require('electron-updater');

// ─────────────────── Single instance lock ───────────────────
// Prevent two Electron processes (and thus two server.py instances) from running
// at the same time — e.g. when the user double-clicks the exe while it's already open.
const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
}

// ─────────────────── Runtime config (filled in whenReady) ───────────────────

let SERVER_HOST   = '127.0.0.1';
let SERVER_PORT   = 8000;
let SERVER_URL    = '';           // set after reading app.yaml

const POLL_INTERVAL  = 500;      // ms between readiness checks
const POLL_MAX_TRIES = 60;       // 30 s total wait

let mainWindow       = null;
let setupWizardWindow = null;
let splashWindow     = null;
let tray             = null;
let serverProcess    = null;
let isQuitting       = false;
let currentSplashPct = 0;
let currentSplashMsg = 'Starting...';
let splashReady      = false;          // true after ready-to-show fires
let splashQueue      = [];             // buffered updates before window is ready

// ─────────────────── Config helpers ───────────────────

function getAppRoot() {
  return app.isPackaged ? process.resourcesPath : __dirname;
}

/**
 * Parse host and port from config/app.yaml without a full YAML parser.
 * Falls back to 127.0.0.1:8000 if the file is missing or malformed.
 */
function readServerConfig(appRoot) {
  try {
    const yaml = fs.readFileSync(path.join(appRoot, 'config', 'app.yaml'), 'utf8');
    const portMatch = yaml.match(/^port:\s*(\d+)/m);
    const hostMatch = yaml.match(/^host:\s*["']?([^\s"'#\r\n]+)["']?/m);
    return {
      port: portMatch ? parseInt(portMatch[1], 10) : 7788,
      host: hostMatch ? hostMatch[1] : '127.0.0.1',
    };
  } catch (_) {
    return { port: 7788, host: '127.0.0.1' };
  }
}

/**
 * Read HTTP_PROXY / HTTPS_PROXY from project .env (same keys as Settings → System).
 * Python server loads .env on startup; Electron-spawned launcher children do not unless merged here.
 */
function readEnvFileProxy(appRoot) {
  const envPath = path.join(appRoot, '.env');
  if (!fs.existsSync(envPath)) return {};
  const out = {};
  try {
    const raw = fs.readFileSync(envPath, 'utf8');
    for (const line of raw.split(/\r?\n/)) {
      const t = line.trim();
      if (!t || t.startsWith('#')) continue;
      const eq = t.indexOf('=');
      if (eq < 1) continue;
      const key = t.slice(0, eq).trim();
      const upper = key.toUpperCase();
      if (upper !== 'HTTP_PROXY' && upper !== 'HTTPS_PROXY') continue;
      let val = t.slice(eq + 1).trim();
      if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
        val = val.slice(1, -1);
      }
      out[upper] = val;
    }
  } catch (_) {
    /* ignore */
  }
  return out;
}

/** Environment for setup-wizard subprocesses (status JSON, pip helper, wizard-download, …). */
function envForWizardChild(appRoot, extra) {
  const env = {
    ...process.env,
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONUNBUFFERED: '1',
    ...(extra || {}),
  };
  const px = readEnvFileProxy(appRoot);
  if (px.HTTP_PROXY) env.HTTP_PROXY = px.HTTP_PROXY;
  if (px.HTTPS_PROXY) env.HTTPS_PROXY = px.HTTPS_PROXY;
  return env;
}

function getPythonCmd(appRoot) {
  // --- PyInstaller Mode (Fallback to python if not compiled) ---
  if (app.isPackaged) {
    const exeName = process.platform === 'win32' ? 'server.exe' : 'server';
    const exePath = path.join(appRoot, exeName);
    if (fs.existsSync(exePath)) return exePath;
  }
  
  // --- Source Mode ---
  const venvPython = process.platform === 'win32'
    ? path.join(appRoot, '.venv', 'Scripts', 'python.exe')
    : path.join(appRoot, '.venv', 'bin', 'python');
  if (fs.existsSync(venvPython)) return venvPython;
  return process.platform === 'win32' ? 'python' : 'python3';
}

function readUiPrefs(appRoot) {
  try {
    const p = path.join(appRoot, 'config', 'ui_prefs.json');
    if (!fs.existsSync(p)) {
      return { show_startup_launcher: true, theme: undefined, locale: undefined };
    }
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    const loc = j.locale;
    return {
      show_startup_launcher: j.show_startup_launcher !== false,
      theme: Object.prototype.hasOwnProperty.call(j, 'theme') ? j.theme : undefined,
      locale: loc === 'zh' || loc === 'en' ? loc : undefined,
    };
  } catch (_) {
    return { show_startup_launcher: true, theme: undefined, locale: undefined };
  }
}

/** 与主界面 index.html 一致：ui_prefs 无 theme 键或值为 null/undefined 时默认 light；空字符串 = 深紫。 */
function getWizardResolvedTheme(appRoot) {
  const prefs = readUiPrefs(appRoot);
  if (prefs.theme === undefined || prefs.theme === null) {
    return 'light';
  }
  let t = String(prefs.theme);
  if (t === 'velvet_legacy') return 'velvet';
  return t;
}

/** 启动器首屏语言：配置文件 → 系统区域 → 默认 en（与主界面 inferLocaleFromNavigator 非中文即 en 一致） */
function getWizardInitialLocale(appRoot) {
  const prefs = readUiPrefs(appRoot);
  if (prefs.locale === 'zh' || prefs.locale === 'en') return prefs.locale;
  try {
    const al = String(app.getLocale() || '').toLowerCase();
    if (al.startsWith('zh')) return 'zh';
    return 'en';
  } catch (_) {
    return 'en';
  }
}

/** 与 data-theme 大致匹配的窗口底色，减少闪屏（启动器在注入主题前） */
function approxWizardBackgroundColor(theme) {
  const t = theme === undefined || theme === null ? 'light' : String(theme);
  if (t === 'light') return '#f0eee8';
  if (t === 'cursor') return '#1c1c1c';
  if (t === 'midnight') return '#0b0f19';
  if (t === 'nord') return '#2e3440';
  if (t === 'matcha') return '#0d1f18';
  if (t === 'velvet') return '#08080e';
  if (t === 'warm') return '#1c1710';
  if (t === 'rose') return '#1a0a0f';
  if (t === 'synthwave') return '#0f0612';
  if (t === 'ink') return '#0c0c0c';
  if (t === '') return '#0d0d1a';
  return '#0d0d1a';
}

function isServerBinary(cmd) {
  const b = path.basename(cmd || '').toLowerCase();
  return b === 'server.exe' || b === 'server';
}

function getServerEntryForWizardCli(appRoot) {
  const py = getPythonCmd(appRoot);
  if (isServerBinary(py)) return { cmd: py, argsBase: [] };
  return { cmd: py, argsBase: [path.join(appRoot, 'server.py')] };
}

function getSetupHelperScriptPath() {
  const root = app.isPackaged ? process.resourcesPath : __dirname;
  return path.join(root, 'scripts', 'setup_wizard_helper.py');
}

function getPipRunnerPython(appRoot) {
  if (app.isPackaged && process.platform === 'win32') {
    const embed = path.join(appRoot, 'python_embed', 'python.exe');
    if (fs.existsSync(embed)) return embed;
  }
  const win = process.platform === 'win32';
  const venvPy = path.join(appRoot, '.venv', win ? 'Scripts' : 'bin', win ? 'python.exe' : 'python');
  if (fs.existsSync(venvPy)) return venvPy;
  return win ? 'python' : 'python3';
}

function runWizardStatusJson(appRoot) {
  return new Promise((resolve, reject) => {
    const { cmd, argsBase } = getServerEntryForWizardCli(appRoot);
    const args = [...argsBase, '--setup-status-json'];
    const child = spawn(cmd, args, {
      cwd: appRoot,
      env: envForWizardChild(appRoot),
    });
    let out = '';
    let err = '';
    child.stdout.on('data', (d) => { out += d.toString(); });
    child.stderr.on('data', (d) => { err += d.toString(); });
    child.on('error', (e) => reject(e));
    child.on('close', (code) => {
      if (code !== 0) {
        reject(new Error(err.trim() || `status subprocess exited ${code}`));
        return;
      }
      const lines = out.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      let payload = null;
      for (let i = lines.length - 1; i >= 0; i -= 1) {
        if (lines[i].startsWith('{')) {
          try {
            payload = JSON.parse(lines[i]);
            break;
          } catch (_) { /* continue */ }
        }
      }
      if (payload) resolve(payload);
      else reject(new Error('invalid JSON from --setup-status-json'));
    });
  });
}

function spawnWizardServerJsonStdin(appRoot, extraArgs, stdinStr, onEventLine) {
  const { cmd, argsBase } = getServerEntryForWizardCli(appRoot);
  const child = spawn(cmd, [...argsBase, ...extraArgs], {
    cwd: appRoot,
    env: envForWizardChild(appRoot),
  });
  if (stdinStr != null && child.stdin) {
    child.stdin.write(stdinStr);
    child.stdin.end();
  }
  attachWizardStdoutParser(child, onEventLine);
  child.stderr.on('data', (d) => process.stderr.write(d));
  return child;
}

function attachWizardStdoutParser(child, onEventLine) {
  if (!child || !child.stdout) return;
  child.stdout.on('data', (buf) => {
    buf.toString().split('\n').forEach((line) => {
      const s = line.trim();
      if (s.startsWith('SETUP_EVENT:')) {
        try {
          const j = JSON.parse(s.slice('SETUP_EVENT:'.length));
          if (typeof onEventLine === 'function') onEventLine(j);
        } catch (_) { /* ignore */ }
      } else if (s.startsWith('SETUP_RESULT:')) {
        try {
          const j = JSON.parse(s.slice('SETUP_RESULT:'.length));
          if (typeof onEventLine === 'function') onEventLine({ type: 'setup_result', result: j });
        } catch (_) { /* ignore */ }
      }
    });
  });
}

function spawnWizardPipHelper(appRoot, pipSpec, onEventLine) {
  const script = getSetupHelperScriptPath();
  if (!fs.existsSync(script)) {
    if (typeof onEventLine === 'function') onEventLine({ type: 'pip', phase: 'error', error: 'helper script missing' });
    return null;
  }
  const py = getPipRunnerPython(appRoot);
  const payload = JSON.stringify(pipSpec);
  const child = spawn(py, [script, 'pip-install', payload], {
    cwd: appRoot,
    env: envForWizardChild(appRoot, { SHIKIGAMI_APP_ROOT: appRoot }),
  });
  attachWizardStdoutParser(child, onEventLine);
  child.stderr.on('data', (d) => process.stderr.write(d));
  return child;
}

let wizardUserLaunched = false;
/** @type {null | (() => void)} */
let wizardFlowResolve = null;
let activeWizardChild = null;

function forwardWizardEvent(wc, obj) {
  if (wc && !wc.isDestroyed()) wc.send('setup-wizard:event', obj);
}

function createSetupWizardWindow() {
  const appRoot = getAppRoot();
  const htmlPath = path.join(appRoot, 'static', 'setup_wizard.html');
  if (!fs.existsSync(htmlPath)) {
    console.warn('[electron] setup_wizard.html missing, skipping launcher');
    return;
  }
  const uiPrefs = readUiPrefs(appRoot);
  const themeVal = getWizardResolvedTheme(appRoot);

  const W = 920;
  const H = 720;
  const primary = screen.getPrimaryDisplay();
  const { width: workW, height: workH } = primary.workAreaSize;
  const x = Math.floor((workW - W) / 2) + primary.workArea.x;
  const y = Math.floor((workH - H) / 2) + primary.workArea.y;

  setupWizardWindow = new BrowserWindow({
    width: W,
    height: H,
    x,
    y,
    show: false,
    backgroundColor: approxWizardBackgroundColor(themeVal),
    icon: getAppIcon(),
    title: 'Shikigami Protocol — 启动器',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
  });
  setupWizardWindow.setMenuBarVisibility(false);
  setupWizardWindow.loadFile(htmlPath);
  setupWizardWindow.once('ready-to-show', () => {
    if (setupWizardWindow && !setupWizardWindow.isDestroyed()) setupWizardWindow.show();
  });
  setupWizardWindow.on('closed', () => {
    setupWizardWindow = null;
    if (activeWizardChild && !activeWizardChild.killed) {
      try { activeWizardChild.kill(); } catch (_) {}
      activeWizardChild = null;
    }
    if (!wizardUserLaunched && !isQuitting) {
      isQuitting = true;
      app.quit();
    }
  });
}

// ─────────────────── Port cleanup ───────────────────

/**
 * On Windows: find and kill any process LISTENING on the given port.
 * Waits 600 ms after kill so the OS has time to release the socket.
 */
function killPortOwner(port) {
  return new Promise((resolve) => {
    if (process.platform !== 'win32') { resolve(); return; }

    exec(`netstat -ano | findstr :${port}`, (err, stdout) => {
      if (!stdout) { resolve(); return; }

      const pids = new Set();
      for (const line of stdout.split('\n')) {
        if (!line.includes('LISTENING')) continue;
        const parts = line.trim().split(/\s+/);
        const pid = parts[parts.length - 1];
        if (pid && /^\d+$/.test(pid) && pid !== '0') pids.add(pid);
      }

      if (!pids.size) { resolve(); return; }

      console.log(`[electron] killing stale process(es) on port ${port}: ${[...pids].join(', ')}`);
      for (const pid of pids) {
        spawn('taskkill', ['/F', '/PID', pid], { stdio: 'ignore' });
      }
      setTimeout(resolve, 600); // wait for OS to release port
    });
  });
}

// ─────────────────── Python server ───────────────────

function startServer() {
  const appRoot    = getAppRoot();
  const pythonCmd  = getPythonCmd(appRoot);
  const isCompiled = !pythonCmd.endsWith('python.exe') && !pythonCmd.endsWith('python') && !pythonCmd.endsWith('python3');
  
  const args = isCompiled ? [] : [path.join(appRoot, 'server.py')];

  console.log(`[electron] appRoot:   ${appRoot}`);
  console.log(`[electron] command:   ${pythonCmd}`);
  if (!isCompiled) console.log(`[electron] script:    ${args[0]}`);
  console.log(`[electron] endpoint:  ${SERVER_URL}`);

  serverProcess = spawn(pythonCmd, args, {
    cwd: appRoot,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1', PYTHONUNBUFFERED: '1' },
  });

  serverProcess.stdout.on('data', (d) => {
    const text = d.toString();
    process.stdout.write(`[server] ${text}`);
    // Parse SPLASH:<pct>:<msg> lines emitted by server.py for real-time progress
    text.split('\n').forEach(line => {
      const m = line.match(/^SPLASH:(\d+):(.*)$/);
      if (m) setSplashProgress(parseInt(m[1], 10), m[2].trim());
    });
  });
  serverProcess.stderr.on('data', (d) =>
    process.stderr.write(`[server:err] ${d}`)
  );
  serverProcess.on('exit', (code) => {
    if (!isQuitting) {
      console.error(`[server] exited unexpectedly (code=${code})`);
    }
  });
}

/**
 * Update splash progress by real startup step (not time-based).
 * Stores state so splash shows correct progress when it first appears.
 * @param {number} pct 0–100
 * @param {string} msg status text
 */
function _execSplash(pct, msg) {
  if (!splashWindow || splashWindow.isDestroyed() || !splashWindow.webContents) return;
  const escaped = (msg || '').replace(/'/g, "\\'").replace(/\r?\n/g, ' ');
  splashWindow.webContents.executeJavaScript(
    `window.__setSplashProgress && window.__setSplashProgress(${pct}, '${escaped}')`
  ).catch(() => {});
}

function setSplashProgress(pct, msg) {
  currentSplashPct = pct;
  currentSplashMsg = msg || currentSplashMsg;
  if (!splashWindow || splashWindow.isDestroyed() || !splashWindow.webContents) return;
  if (!splashReady) {
    splashQueue.push({ pct: currentSplashPct, msg: currentSplashMsg });
    return;
  }
  _execSplash(currentSplashPct, currentSplashMsg);
}

/**
 * Read startup progress written by Python server (real steps: 35–100).
 * Returns { pct, msg } or null if file missing/invalid.
 */
function readStartupProgress() {
  try {
    const p = path.join(getAppRoot(), 'startup_progress.json');
    if (!fs.existsSync(p)) return null;
    const raw = fs.readFileSync(p, 'utf8');
    const data = JSON.parse(raw);
    if (typeof data.pct !== 'number' || data.pct < 0) return null;
    return { pct: Math.min(100, data.pct), msg: (data.msg && String(data.msg)) || '' };
  } catch (_) {
    return null;
  }
}

/**
 * Poll GET /sessions until HTTP 200 or retries exhausted.
 * Progress updates now come via stdout (SPLASH: lines) — no file polling needed.
 */
function serverReady() {
  return new Promise((resolve, reject) => {
    let tries = 0;

    function attempt() {
      tries += 1;
      http.get(`${SERVER_URL}/sessions`, (res) => {
        res.resume();
        if (res.statusCode === 200) {
          setSplashProgress(100, 'Opening window...');
          resolve();
        } else {
          scheduleRetry();
        }
      }).on('error', scheduleRetry);
    }

    function scheduleRetry() {
      if (tries >= POLL_MAX_TRIES) {
        setSplashProgress(100, 'Connection timed out, opening window...');
        reject(new Error('Python server did not become ready in time'));
        return;
      }
      setTimeout(attempt, POLL_INTERVAL);
    }

    attempt();
  });
}

function killServer() {
  if (!serverProcess) return;
  const pid = serverProcess.pid;
  serverProcess = null;
  try {
    if (process.platform === 'win32') {
      // execFileSync blocks until taskkill exits — ensures server.exe is dead
      // before Electron exits, so no locked files during uninstall.
      const { execFileSync } = require('child_process');
      try {
        execFileSync('taskkill', ['/pid', String(pid), '/f', '/t'], { stdio: 'ignore', timeout: 5000 });
      } catch (_) { /* process may already be gone */ }
    } else {
      process.kill(pid, 'SIGTERM');
    }
  } catch (_) { /* best-effort */ }
}

// ─────────────────── App icon ───────────────────

/**
 * Generate a round purple circle icon programmatically (no external file needed).
 * Uses nativeImage.createFromBitmap which accepts raw RGBA pixel data.
 * @param {number} size  Side length in pixels (power of 2 recommended)
 */
function makeIcon(size) {
  const buf = Buffer.alloc(size * size * 4, 0); // all transparent
  const cx = size / 2 - 0.5;
  const cy = size / 2 - 0.5;
  const r  = size / 2 - 1.5;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const dist = Math.hypot(x - cx, y - cy);
      if (dist > r) continue;
      const i = (y * size + x) * 4;
      // Indigo-purple gradient: darker in center, brighter towards edge
      const t = dist / r;
      buf[i]     = Math.round(80  + t * 60);  // R
      buf[i + 1] = Math.round(60  + t * 40);  // G
      buf[i + 2] = Math.round(200 + t * 30);  // B
      buf[i + 3] = 255;                        // A
    }
  }
  return nativeImage.createFromBitmap(buf, { width: size, height: size });
}

// Prefer assets/icon.ico or assets/shikigami_protocol_icon.png; else generated circle
function getAppIcon() {
  const appRoot = getAppRoot();
  const icoPath = path.join(appRoot, 'assets', 'icon.ico');
  const pngPath = path.join(appRoot, 'assets', 'shikigami_protocol_icon.png');
  if (fs.existsSync(icoPath)) return nativeImage.createFromPath(icoPath);
  if (fs.existsSync(pngPath)) return nativeImage.createFromPath(pngPath);
  return makeIcon(64);
}
const TRAY_ICON = makeIcon(16);

// ─────────────────── Splash (show immediately, before server ready) ───────────────────

function createSplashWindow() {
  const appRoot = getAppRoot();
  const splashPath = path.join(appRoot, 'static', 'splash.html');
  if (!fs.existsSync(splashPath)) {
    console.warn('[electron] static/splash.html not found, skipping splash');
    return;
  }
  const SPLASH_SIZE = 900;
  const primary = screen.getPrimaryDisplay();
  const { width: workW, height: workH } = primary.workAreaSize;
  const splashX = Math.floor((workW - SPLASH_SIZE) / 2) + primary.workArea.x;
  const splashY = Math.floor((workH - SPLASH_SIZE) / 2) + primary.workArea.y;

  splashWindow = new BrowserWindow({
    width: SPLASH_SIZE,
    height: SPLASH_SIZE,
    x: splashX,
    y: splashY,
    frame: false,
    transparent: true,
    resizable: false,
    skipTaskbar: true,
    hasShadow: false,
    backgroundColor: '#00000000',
    show: false,
    icon: getAppIcon(),
    title: 'Shikigami Protocol',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });
  splashWindow.loadFile(splashPath);
  splashWindow.setMenuBarVisibility(false);
  splashWindow.once('ready-to-show', () => {
    splashWindow.setBounds({ x: splashX, y: splashY, width: SPLASH_SIZE, height: SPLASH_SIZE });
    splashWindow.show();
    splashReady = true;
    // Replay buffered SPLASH updates with staggered delays so the bar animates
    const queue = splashQueue.splice(0);
    if (queue.length > 0) {
      queue.forEach(({ pct, msg }, i) => {
        setTimeout(() => _execSplash(pct, msg), i * 220);
      });
    } else {
      _execSplash(currentSplashPct, currentSplashMsg);
    }
  });
  splashWindow.on('closed', () => { splashWindow = null; });
}

function closeSplashWindow() {
  if (splashWindow && !splashWindow.isDestroyed()) {
    splashWindow.close();
    splashWindow = null;
  }
}

// ─────────────────── Browser window ───────────────────

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 760,
    minWidth: 800,
    minHeight: 580,
    backgroundColor: '#1a1a2e',
    show: false,
    icon: getAppIcon(),
    title: 'Shikigami Protocol',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
  });

  mainWindow.loadURL(SERVER_URL);
  mainWindow.setMenuBarVisibility(false);

  // Handle target="_blank" links: open docs-viewer pages in a proper child window.
  // Any other same-origin navigation is allowed in-place; deny everything else.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.includes('/docs-viewer.html')) {
      return {
        action: 'allow',
        overrideBrowserWindowOptions: {
          width: 920,
          height: 720,
          autoHideMenuBar: true,
          icon: getAppIcon(),
          webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true },
        },
      };
    }
    // Open external URLs in the system browser, deny new windows for everything else.
    if (/^https?:\/\//i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
    mainWindow.focus();
    closeSplashWindow();
  });

  mainWindow.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });
}

// ─────────────────── System tray ───────────────────

function createTray() {
  const appRoot = getAppRoot();
  const icoPath = path.join(appRoot, 'assets', 'icon.ico');
  const pngPath = path.join(appRoot, 'assets', 'shikigami_protocol_icon.png');
  let icon = TRAY_ICON;
  if (fs.existsSync(icoPath)) {
    icon = nativeImage.createFromPath(icoPath);
  } else if (fs.existsSync(pngPath)) {
    icon = nativeImage.createFromPath(pngPath);
    const small = icon.resize({ width: 16, height: 16 });
    if (!small.isEmpty()) icon = small;
  }

  tray = new Tray(icon);
  tray.setToolTip('Shikigami Protocol');

  tray.setContextMenu(Menu.buildFromTemplate([
    {
      label: '显示窗口',
      click: () => { if (mainWindow) { mainWindow.show(); mainWindow.focus(); } },
    },
    { type: 'separator' },
    { label: '退出 Shikigami', click: () => quitApp() },
  ]));

  tray.on('double-click', () => {
    if (mainWindow) { mainWindow.show(); mainWindow.focus(); }
  });
}

// ─────────────────── Clean quit ───────────────────

function quitApp() {
  isQuitting = true;
  killServer();
  if (tray) { tray.destroy(); tray = null; }
  app.quit();
}

// ─────────────────── IPC handlers ───────────────────

ipcMain.handle('take-screenshot', async () => {
  try {
    const sources = await desktopCapturer.getSources({
      types: ['screen'],
      thumbnailSize: { width: 1920, height: 1080 },
    });
    if (!sources || sources.length === 0) return null;
    return 'data:image/png;base64,' + sources[0].thumbnail.toPNG().toString('base64');
  } catch (e) {
    console.error('[screenshot] error:', e.message);
    return null;
  }
});

ipcMain.handle('quit-app', () => quitApp());

ipcMain.handle('open-external-url', (_event, url) => {
  if (typeof url === 'string' && (url.startsWith('https://') || url.startsWith('http://'))) {
    shell.openExternal(url);
  }
});

ipcMain.handle('check-for-updates', () => {
  if (!app.isPackaged) return;
  autoUpdater.checkForUpdates().catch((e) =>
    console.error('[updater] check failed:', e.message)
  );
});

ipcMain.handle('download-update', () => {
  autoUpdater.downloadUpdate().catch((e) =>
    console.error('[updater] download failed:', e.message)
  );
});

ipcMain.handle('install-update', () => {
  autoUpdater.quitAndInstall();
});

// ─────────────────── Setup wizard (pre-server) ───────────────────

ipcMain.on('setup-wizard:get-locale-bootstrap', (event) => {
  try {
    event.returnValue = getWizardInitialLocale(getAppRoot());
  } catch (_) {
    event.returnValue = 'en';
  }
});

ipcMain.on('setup-wizard:get-theme-bootstrap', (event) => {
  try {
    event.returnValue = getWizardResolvedTheme(getAppRoot());
  } catch (_) {
    event.returnValue = 'light';
  }
});

ipcMain.handle('setup-wizard:get-status', async () => {
  const appRoot = getAppRoot();
  try {
    const data = await runWizardStatusJson(appRoot);
    return { ok: true, data };
  } catch (e) {
    return { ok: false, error: e.message || String(e) };
  }
});

ipcMain.handle('setup-wizard:proceed', async () => {
  wizardUserLaunched = true;
  if (typeof wizardFlowResolve === 'function') {
    const fn = wizardFlowResolve;
    wizardFlowResolve = null;
    fn();
  }
  if (setupWizardWindow && !setupWizardWindow.isDestroyed()) setupWizardWindow.close();
  return { ok: true };
});

ipcMain.handle('setup-wizard:pick-stt-model', async (event) => {
  const win = BrowserWindow.fromWebContents(event.sender);
  const parent = win && !win.isDestroyed()
    ? win
    : (setupWizardWindow && !setupWizardWindow.isDestroyed() ? setupWizardWindow : BrowserWindow.getFocusedWindow());
  if (!parent) return { path: '' };
  const r = await dialog.showOpenDialog(parent, {
    title: 'Select SenseVoice model folder (tokens.txt + model.onnx) / 选择模型文件夹',
    properties: ['openDirectory'],
  });
  if (r.canceled || !r.filePaths || !r.filePaths[0]) return { path: '' };
  return { path: r.filePaths[0] };
});

ipcMain.handle('setup-wizard:save-gptsovits-dir', async (_event, dirStr) => {
  const appRoot = getAppRoot();
  const payload = JSON.stringify({ dir: String(dirStr || '') });
  const { cmd, argsBase } = getServerEntryForWizardCli(appRoot);
  return new Promise((resolve) => {
    const child = spawn(cmd, [...argsBase, '--wizard-save-gptsovits-dir'], {
      cwd: appRoot,
      env: envForWizardChild(appRoot),
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    if (child.stdin) {
      child.stdin.write(payload, 'utf8');
      child.stdin.end();
    }
    let out = '';
    child.stdout.on('data', (d) => { out += d.toString(); });
    child.stderr.on('data', (d) => { process.stderr.write(d); });
    child.on('error', () => resolve({ ok: false, error: 'spawn failed' }));
    child.on('close', () => {
      const lines = out.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      for (let i = lines.length - 1; i >= 0; i -= 1) {
        const s = lines[i];
        if (s.startsWith('SETUP_RESULT:')) {
          try {
            resolve(JSON.parse(s.slice('SETUP_RESULT:'.length)));
            return;
          } catch (_) { /* continue */ }
        }
      }
      resolve({ ok: false, error: 'no SETUP_RESULT' });
    });
  });
});

ipcMain.on('setup-wizard:start-op', (event, payload) => {
  const wc = event.sender;
  const appRoot = getAppRoot();
  const onEv = (j) => forwardWizardEvent(wc, j);

  if (activeWizardChild && !activeWizardChild.killed) {
    try { activeWizardChild.kill(); } catch (_) {}
    activeWizardChild = null;
  }

  const op = (payload && payload.op) || '';

  if (op === 'pip-install') {
    activeWizardChild = spawnWizardPipHelper(appRoot, {
      packages: payload.packages || [],
      target: payload.target || '',
      index_url: payload.index_url || '',
    }, onEv);
    if (activeWizardChild) {
      activeWizardChild.on('close', (code) => {
        activeWizardChild = null;
        forwardWizardEvent(wc, { type: 'child_done', op: 'pip-install', exitCode: code });
      });
    }
    return;
  }

  if (op === 'pip-uninstall') {
    const body = JSON.stringify({
      packages: payload.packages || [],
      dirs: payload.dirs || [],
    });
    activeWizardChild = spawnWizardServerJsonStdin(appRoot, ['--wizard-pip-uninstall'], body, onEv);
    if (activeWizardChild) {
      activeWizardChild.on('close', (code) => {
        activeWizardChild = null;
        forwardWizardEvent(wc, { type: 'child_done', op: 'pip-uninstall', exitCode: code });
      });
    }
    return;
  }

  if (op === 'download') {
    const bid = payload.bundle_id || '';
    const src = payload.source || 'auto';
    const { cmd, argsBase } = getServerEntryForWizardCli(appRoot);
    activeWizardChild = spawn(cmd, [...argsBase, '--wizard-download', bid, src], {
      cwd: appRoot,
      env: envForWizardChild(appRoot),
    });
    attachWizardStdoutParser(activeWizardChild, onEv);
    activeWizardChild.stderr.on('data', (d) => process.stderr.write(d));
    activeWizardChild.on('close', (code) => {
      activeWizardChild = null;
      forwardWizardEvent(wc, { type: 'child_done', op: 'download', exitCode: code });
    });
    return;
  }

  if (op === 'apply-stt') {
    const mp = (payload.model_path || '').trim();
    const { cmd, argsBase } = getServerEntryForWizardCli(appRoot);
    activeWizardChild = spawn(cmd, [...argsBase, '--wizard-apply-stt', mp], {
      cwd: appRoot,
      env: envForWizardChild(appRoot),
    });
    activeWizardChild.stderr.on('data', (d) => process.stderr.write(d));
    activeWizardChild.on('close', (code) => {
      activeWizardChild = null;
      forwardWizardEvent(wc, { type: 'child_done', op: 'apply-stt', exitCode: code });
    });
    return;
  }

  if (op === 'launch-gptsovits') {
    const { cmd, argsBase } = getServerEntryForWizardCli(appRoot);
    activeWizardChild = spawn(cmd, [...argsBase, '--wizard-launch-gptsovits'], {
      cwd: appRoot,
      env: envForWizardChild(appRoot),
    });
    activeWizardChild.stderr.on('data', (d) => process.stderr.write(d));
    activeWizardChild.on('close', (code) => {
      activeWizardChild = null;
      forwardWizardEvent(wc, { type: 'child_done', op: 'launch-gptsovits', exitCode: code });
    });
  }
});

// ─────────────────── App lifecycle ───────────────────

// Disable Electron's HTTP disk cache so local static file changes always load fresh.
// The backend is a local FastAPI server; caching only causes stale HTML/CSS/JS issues.
app.commandLine.appendSwitch('disable-http-cache');

// Set Windows App User Model ID — required for correct Task Manager grouping + icon
if (process.platform === 'win32') {
  app.setAppUserModelId('com.shikigami.protocol');
}

app.whenReady().then(async () => {
  const appRoot = getAppRoot();

  // 1. First-run: copy app.yaml.example → app.yaml if the latter doesn't exist
  const yamlPath    = path.join(appRoot, 'config', 'app.yaml');
  const examplePath = path.join(appRoot, 'config', 'app.yaml.example');
  if (!fs.existsSync(yamlPath) && fs.existsSync(examplePath)) {
    try {
      fs.copyFileSync(examplePath, yamlPath);
      console.log('[electron] created config/app.yaml from example');
    } catch (e) {
      console.warn('[electron] could not copy app.yaml.example:', e.message);
    }
  }

  // 2. Read actual host/port from config/app.yaml
  const cfg = readServerConfig(appRoot);
  SERVER_HOST = cfg.host;
  SERVER_PORT = cfg.port;
  const clientHost = (SERVER_HOST === '0.0.0.0' || SERVER_HOST === '::') ? '127.0.0.1' : SERVER_HOST;
  SERVER_URL  = `http://${clientHost}:${SERVER_PORT}`;
  console.log(`[electron] server URL: ${SERVER_URL}`);

  const uiPrefs = readUiPrefs(appRoot);
  const wizardHtml = path.join(appRoot, 'static', 'setup_wizard.html');
  const wantWizard = uiPrefs.show_startup_launcher !== false && fs.existsSync(wizardHtml);

  if (wantWizard) {
    wizardUserLaunched = false;
    await new Promise((resolve) => {
      wizardFlowResolve = resolve;
      createSetupWizardWindow();
    });
    wizardFlowResolve = null;
  } else {
    wizardUserLaunched = true;
  }

  setSplashProgress(10, 'Config loaded');

  // 3. Kill any stale process still holding the port
  await killPortOwner(SERVER_PORT);
  setSplashProgress(20, 'Releasing port...');

  // 4. Show splash (loads local static/splash.html); ready-to-show will sync current progress
  createSplashWindow();

  // 5. Start server and wait for it
  try { fs.writeFileSync(path.join(appRoot, 'startup_progress.json'), '{"pct":0,"msg":""}', 'utf8'); } catch (_) {}
  startServer();
  setSplashProgress(30, 'Waiting for service to be ready...');
  console.log('[electron] waiting for Python server to become ready...');

  try {
    await serverReady();
    console.log('[electron] server ready — opening main window');
  } catch (e) {
    console.error('[electron]', e.message);
    closeSplashWindow();
  }

  createWindow();
  createTray();

  // ── Auto-updater ────────────────────────────────────────────────────────────
  if (app.isPackaged) {
    autoUpdater.autoDownload = false;
    autoUpdater.autoInstallOnAppQuit = true;

    const sendUpdate = (status, data = {}) => {
      if (mainWindow && !mainWindow.isDestroyed())
        mainWindow.webContents.send('update-status', { status, ...data });
    };

    autoUpdater.on('checking-for-update',  ()    => sendUpdate('checking'));
    autoUpdater.on('update-available',     (i)   => sendUpdate('available',   { version: i.version }));
    autoUpdater.on('update-not-available', ()    => sendUpdate('not-available'));
    autoUpdater.on('download-progress',    (p)   => sendUpdate('downloading', { percent: Math.round(p.percent) }));
    autoUpdater.on('update-downloaded',    (i)   => sendUpdate('ready',       { version: i.version }));
    autoUpdater.on('error',                (e)   => {
      console.error('[updater] error:', e);
      sendUpdate('error', { message: e.message });
    });

    setTimeout(() => autoUpdater.checkForUpdates().catch(() => {}), 5000);
  }

  app.on('activate', () => {
    if (mainWindow) { mainWindow.show(); mainWindow.focus(); }
  });

  app.on('second-instance', () => {
    if (setupWizardWindow && !setupWizardWindow.isDestroyed()) {
      setupWizardWindow.show();
      setupWizardWindow.focus();
      return;
    }
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
});

app.on('before-quit', () => {
  isQuitting = true;
  killServer();
});

app.on('window-all-closed', () => {
  // Intentionally empty — tray keeps the app alive
});
