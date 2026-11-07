// Offline protocol fixture only. Not mounted by the application or exposed to a host port.
import http from 'node:http';
import { randomUUID } from 'node:crypto';
import { installControls } from '../controls.mjs';
import { buildHistorySeed } from '../history.mjs';

export const name = 'shikigami-contract-fixture';
export const inject = ['agents', 'systemPrompt', 'tools', 'approval'];

export function apply(ctx) {
  const bindings = new Map(), revisions = new Map(), requests = new Set(), usedSessions = new Set(), handles = new Map();
  let startQueue = Promise.resolve();
  const events = [], audit = [];
  const owner = agent => bindings.get(agent)?.binding;
  const publish = event => events.push({ ...event, sequence: events.length + 1 });
  const control = installControls(ctx, { owner, publish, approvalTimeoutMs: 500 });
  const agentOptions = { provider: 'fixture', model: 'fixture-model', maxTokens: 128 };
  function setupFor(system) {
    return agentCtx => {
      agentCtx.systemPrompt.section({ name: 'deployment:persona-prefix', order: -100,
        text: system, interpolate: false });
      agentCtx.systemPrompt.suppressRuntimeContext();
      agentCtx.tools.restrict({ allow: ['read'] });
      agentCtx.tools.guard(exec => exec.name !== 'read' || exec.arguments?.file_path !== '/workspace/fixtures/readme.txt'
        ? 'Only the synthetic fixture file is available in this protocol test.' : undefined);
    };
  }
  ctx.on('session/event', (session, event) => {
    if (event.type === 'turn/end') {
      const record = bindings.get(ctx.agents.get(session.id));
      if (record) record.terminal = true;
    }
    if (['user/message', 'approval/asked', 'approval/decided', 'turn/end'].includes(event.type)) {
      audit.push({ session_id: session.id, type: event.type, data: event.data });
    }
  });

  function getRun(body) {
    const agent = ctx.agents.get(body.session_id), bound = agent && owner(agent);
    if (!bound || !['profile_id', 'request_id', 'context_revision', 'session_id'].every(k => body[k] === bound[k])
        || revisions.get(bound.profile_id) !== bound.context_revision) throw new Error('stale or mismatched run');
    return agent;
  }

  async function dispatch(path, body) {
    if (path === '/inspect') return { events, audit, tools: ctx.tools.schemas().map(t => t.name) };
    if (path === '/start') {
      if (body.schema_version !== 1 || !['user', 'reflection', 'proactive'].includes(body.origin)
          || !Number.isSafeInteger(body.context_revision) || body.context_revision < 0
          || !['profile_id', 'request_id', 'session_id'].every(k => typeof body[k] === 'string' && body[k].length > 0 && body[k].length <= 256)) {
        throw new Error('invalid context envelope');
      }
      const { seed, trigger } = buildHistorySeed(body);
      if (ctx.agents.get(body.session_id) || usedSessions.has(body.session_id)) throw new Error('requires a fresh agent identity');
      const previous = revisions.get(body.profile_id) ?? -1;
      if (body.context_revision < previous || requests.has(body.request_id)) throw new Error('stale revision or duplicate request');
      // Reserve before cancellation yields; a failed attempt is not retried on a
      // possibly partially initialized native session.
      usedSessions.add(body.session_id);
      requests.add(body.request_id);
      // New revisions invalidate older sessions, including their pending approvals.
      revisions.set(body.profile_id, body.context_revision);
      if (body.context_revision > previous) {
        for (const [old, record] of bindings) {
          if (record.binding.profile_id === body.profile_id) {
            if (!record.terminal) {
              publish({ ...record.binding, type: 'status', status: 'cancelling',
                source: { kind: 'context-invalidation' } });
              old.cancel({ kind: 'hook', reason: 'context invalidated' });
              await old.whenIdle();
            }
            // HTTP authority was withdrawn by the revision change above.
            // Keep the original event binding until the actual native terminal
            // event closes its UI task; only then retire the event subscription.
            control.revoke(old);
            bindings.delete(old);
            const oldHandle = handles.get(old.id);
            if (oldHandle) { await oldHandle.dispose(); handles.delete(old.id); }
          }
        }
      }
      const binding = Object.freeze(Object.fromEntries(
        ['profile_id', 'request_id', 'context_revision', 'session_id'].map(k => [k, body[k]])));
      // Public native creation loads a validated event seed before publication.
      // The SDK still owns the runtime; this plugin owns/disposes its agents.
      const setup = setupFor(body.messages[0].content);
      const handle = await ctx.agents.create({ sessionId: body.session_id, seed,
        meta: { cwd: '/workspace' }, agentOptions, setup });
      const agent = handle.agent;
      handles.set(agent.id, handle);
      bindings.set(agent, { binding, terminal: false, setup });
      agent.followup({ id: randomUUID(), role: 'user',
        content: [{ type: 'text', text: trigger }],
        source: { kind: body.origin === 'user' ? 'user' : `shikigami-${body.origin}` } });
      return { accepted: true, ...binding };
    }
    if (path === '/roundtrip') {
      // Diagnostic only: close the native writer, restore its JSONL through
      // the native API, compare context, then dispose the restored agent.
      const agent = getRun(body), record = bindings.get(agent);
      if (!record.terminal) throw new Error('roundtrip requires a completed run');
      await agent.whenIdle();
      const before = agent.session.deriveMessages();
      await handles.get(agent.id).dispose();
      handles.delete(agent.id); bindings.delete(agent);
      const restored = await ctx.agents.resume({ resumeSessionId: agent.id, agentOptions, setup: record.setup });
      try { return { before, after: restored.agent.session.deriveMessages() }; }
      finally { await restored.dispose(); }
    }
    if (path === '/answer') {
      getRun(body);
      return { accepted: control.answer(body, body.approval_id, body.outcome) };
    }
    if (path === '/cancel') {
      getRun(body);
      return { accepted: await control.cancel(body) };
    }
    throw new Error('unknown endpoint');
  }

  const server = http.createServer(async (req, res) => {
    try {
      if (req.method !== 'POST' || !process.env.CONTRACT_TOKEN
          || req.headers.authorization !== `Bearer ${process.env.CONTRACT_TOKEN}`) {
        res.writeHead(403).end(); return;
      }
      let size = 0; const chunks = [];
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 1024 * 1024) throw new Error('request too large');
        chunks.push(chunk);
      }
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      let result;
      if (req.url === '/start' || req.url === '/roundtrip') {
        // Serialize context transitions; /inspect, /answer and /cancel remain
        // responsive while a prior agent converges to idle.
        const pending = startQueue.then(() => dispatch(req.url, body));
        startQueue = pending.catch(() => {});
        result = await pending;
      } else result = await dispatch(req.url, body);
      res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(result));
    } catch (error) {
      res.writeHead(409, { 'Content-Type': 'application/json' }).end(JSON.stringify({ error: error.message }));
    }
  });
  server.requestTimeout = 5000;
  server.listen(8091, '127.0.0.1');
  ctx.on('dispose', () => new Promise(resolve => server.close(resolve)));
  ctx.on('dispose', async () => { for (const handle of handles.values()) await handle.dispose(); handles.clear(); });
}
