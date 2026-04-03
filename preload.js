'use strict';

/**
 * preload.js — Shikigami Protocol Electron preload script
 *
 * Runs in the renderer's context with access to Node APIs, but exposes
 * only a narrow, explicit surface to the web page via contextBridge.
 * Security baseline: nodeIntegration: false + contextIsolation: true + preload
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  /**
   * Capture the primary screen.
   * Returns a base64 PNG data URL string, or null on failure.
   * @returns {Promise<string|null>}
   */
  captureScreen: () => ipcRenderer.invoke('take-screenshot'),

  /**
   * Gracefully quit the application (kills Python server, destroys tray).
   * @returns {Promise<void>}
   */
  quitApp: () => ipcRenderer.invoke('quit-app'),

  /**
   * Open a URL in the system default browser.
   * @param {string} url
   */
  openExternal: (url) => ipcRenderer.invoke('open-external-url', url),

  /**
   * True when running inside Electron (absent in browser).
   */
  isElectron: true,

  /** Trigger an update check (no-op outside packaged app). */
  checkForUpdates: () => ipcRenderer.invoke('check-for-updates'),

  /** Start downloading the available update. */
  downloadUpdate: () => ipcRenderer.invoke('download-update'),

  /** Quit and install the downloaded update. */
  installUpdate: () => ipcRenderer.invoke('install-update'),

  /**
   * Listen for update status events from main process.
   * cb receives { status, version?, percent?, message? }
   * Returns an unsubscribe function.
   */
  onUpdateStatus: (cb) => {
    const handler = (_e, data) => cb(data);
    ipcRenderer.on('update-status', handler);
    return () => ipcRenderer.removeListener('update-status', handler);
  },

  /** 启动前安装器（仅 Electron） */
  /** 同步：在页面最早脚本中读取，优先级见 main getWizardInitialLocale */
  getSetupWizardLocaleBootstrap: () => {
    try {
      const v = ipcRenderer.sendSync('setup-wizard:get-locale-bootstrap');
      return v === 'zh' || v === 'en' ? v : '';
    } catch (_) {
      return '';
    }
  },
  setupWizardGetStatus: () => ipcRenderer.invoke('setup-wizard:get-status'),
  setupWizardProceed: () => ipcRenderer.invoke('setup-wizard:proceed'),
  setupWizardPickSttModel: () => ipcRenderer.invoke('setup-wizard:pick-stt-model'),
  setupWizardStartOp: (payload) => ipcRenderer.send('setup-wizard:start-op', payload),
  onSetupWizardEvent: (cb) => {
    const handler = (_e, data) => cb(data);
    ipcRenderer.on('setup-wizard:event', handler);
    return () => ipcRenderer.removeListener('setup-wizard:event', handler);
  },
});
