import path from 'node:path';
import { realpathSync, existsSync } from 'node:fs';

// This scopes the upstream tools; it does not implement filesystem operations.
export function workspaceGuard(exec) {
  if (exec.name === 'bash') {
    const cwd = path.posix.resolve('/workspace', exec.arguments?.workdir || '.');
    if (cwd !== '/workspace' && !cwd.startsWith('/workspace/')) return 'Commands must start in the character workspace';
  }
  if (!['read', 'write', 'edit', 'glob', 'grep'].includes(exec.name)) return;
  const args = exec.arguments || {};
  const raw = args.file_path ?? args.path ?? '/workspace';
  if (typeof raw !== 'string' || raw.includes('\0')) return 'Invalid workspace path';
  const target = path.posix.resolve('/workspace', raw);
  if (target !== '/workspace' && !target.startsWith('/workspace/')) return 'Only the character workspace is available';
  let parent = target;
  while (!existsSync(parent) && parent !== '/') parent = path.posix.dirname(parent);
  try {
    const resolved = realpathSync(parent);
    if (resolved !== '/workspace' && !resolved.startsWith('/workspace/')) return 'Workspace links cannot escape';
  } catch { return 'Workspace path is unavailable'; }
}

export function installWebBridge(ctx) {
  return ctx.web.registerFetchProvider({
    id: 'shikigami-bridge', available: () => true,
    async fetch(request, signal) {
      const response = await fetch('http://127.0.0.1:8093/fetch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request), signal,
      });
      if (!response.ok) throw new Error('Web retrieval failed or was stopped');
      return response.json();
    },
  });
}
