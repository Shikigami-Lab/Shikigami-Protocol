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
const path = require('path');

const electronBin = path.join(
  __dirname,
  'node_modules',
  '.bin',
  process.platform === 'win32' ? 'electron.cmd' : 'electron'
);

const child = spawn(electronBin, ['.'], {
  stdio: 'inherit',
  env: { ...process.env, NODE_ENV: 'development' },
});

child.on('exit', (code) => process.exit(code ?? 0));
