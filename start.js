'use strict';

/**
 * start.js — Development launcher for Shikigami Protocol
 *
 * Sets NODE_ENV=development so main.js knows to use __dirname
 * as appRoot (source tree) instead of process.resourcesPath (packaged).
 *
 * Usage:
 *   node start.js
 *   npm run dev
 */

process.env.NODE_ENV = 'development';

const { spawn } = require('child_process');

try {
  // electron exports its real executable path; Windows .cmd shims cannot
  // be spawned directly by Node without a shell.
  const electronBin = require('electron');
  const child = spawn(electronBin, ['.'], {
    cwd: __dirname,
    stdio: 'inherit',
    env: { ...process.env, NODE_ENV: 'development' },
  });

  child.on('error', (error) => {
    console.error('Unable to start Electron. Run npm install and retry:', error.message);
    process.exit(1);
  });
  child.on('exit', (code) => process.exit(code ?? 0));
} catch (error) {
  console.error('Unable to load Electron. Run npm install and retry:', error.message);
  process.exit(1);
}
