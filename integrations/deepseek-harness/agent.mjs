// Public rc2 creation/history/control hooks; execution stays in the native loop.
import http from 'node:http';
import { randomUUID } from 'node:crypto';
import { installControls } from './controls.mjs';
import { buildHistorySeed } from './history.mjs';
import { workspaceGuard, installWebBridge } from './native-access.mjs';
import { installCharacterPrompt } from './prompt-context.mjs';
export const name = 'shikigami-agent';
export const inject = ['agents', 'tools', 'systemPrompt', 'approval', 'web'];
export function apply(ctx) {
  installWebBridge(ctx);
  let handle, binding, started = false, eventSequence = 0;
  const events = [];
  const control = installControls(ctx, { owner: agent => agent === handle?.agent ? binding : undefined,
    publish: event => events.push({ ...event, native_sequence: ++eventSequence }), askTools: [],
    approvalTimeoutMs: 60_000 });
  const server = http.createServer(async (req, res) => {
    try {
      if (req.method === 'GET' && req.url === '/events') {
        res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(events.splice(0))); return;
      }
      const chunks = []; let size = 0;
      for await (const c of req) { size += c.length; if (size > 8_000_000) throw new Error('Context too large'); chunks.push(c); }
      const input = JSON.parse(Buffer.concat(chunks).toString() || '{}');
      if (req.url === '/start') {
        if (started) throw new Error('One request per worker');
        started = true;
        const { envelope, timer_enabled } = input;
        const { seed, trigger } = buildHistorySeed(envelope);
        binding = Object.freeze({ profile_id: envelope.profile_id, request_id: envelope.request_id,
          context_revision: envelope.context_revision, session_id: envelope.request_id });
        const names = [...(timer_enabled ? ['timer_create', 'timer_schedule', 'timer_list', 'timer_cancel'] : []), ...(input.business_tools || [])];
        const tools = [...names.map(n => 'mcp__shikigami__' + n), ...(input.native_tools || [])];
        const available = new Set(ctx.tools.schemas().map(t => t.name));
        if (tools.some(n => !available.has(n))) throw new Error('Timer connection is unavailable');
        handle = await ctx.agents.create({ sessionId: binding.session_id, seed,
          meta: { cwd: '/workspace' },
          agentOptions: { provider: process.env.SHIKI_PROVIDER, model: process.env.SHIKI_MODEL, maxTokens: 4096 },
          setup(agentCtx) {
            // Keep the existing character prompt authoritative, and advertise
            // only the actual scoped MCP schemas, not the SDK coding persona.
            installCharacterPrompt(agentCtx, envelope.messages[0].content);
            agentCtx.tools.restrict({ allow: tools });
            agentCtx.tools.guard(workspaceGuard);
          },
        });
        handle.agent.followup({ id: randomUUID(), role: 'user', content: [{ type: 'text', text: trigger }],
          source: envelope.origin === 'user' ? { kind: 'user' } : { kind: 'shikigami-reflection' } });
        res.end('{"accepted":true}'); return;
      }
      if (!binding) throw new Error('No active request');
      if (req.url === '/cancel') {
        const accepted = await control.cancel(binding);
        res.end(JSON.stringify({ accepted })); return;
      }
      if (req.url === '/approval') {
        const accepted = control.answer(binding, input.approval_id, input.outcome);
        res.end(JSON.stringify({ accepted })); return;
      }
      res.writeHead(404).end();
    } catch (error) { res.writeHead(409, { 'Content-Type': 'application/json' }).end(JSON.stringify({ error: error.message })); }
  });
  server.listen(8091, '127.0.0.1');
  ctx.on('dispose', () => new Promise(resolve => server.close(resolve)));
  ctx.on('dispose', async () => { if (handle) await handle.dispose(); });
}
