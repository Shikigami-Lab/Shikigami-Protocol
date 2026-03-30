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
});
