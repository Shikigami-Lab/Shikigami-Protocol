import { randomUUID } from 'node:crypto';

const BINDING_KEYS = ['profile_id', 'request_id', 'context_revision', 'session_id'];
const sameBinding = (a, b) => !!a && !!b && BINDING_KEYS.every(key => a[key] === b[key]);
function snapshotBinding(value) {
  if (!value || !['profile_id', 'request_id', 'session_id'].every(key =>
    typeof value[key] === 'string' && value[key].length > 0)) return undefined;
  const revision = value.context_revision;
  if (!Number.isSafeInteger(revision) || revision < 0) return undefined;
  return Object.freeze(Object.fromEntries(BINDING_KEYS.map(key => [key, value[key]])));
}

/**
 * rc2 public hooks only. The host binds ownership before enqueueing input and
 * serializes runs per session. publish receives { type, ...binding, source, ... }.
 * context_revision is a nonnegative safe integer, with no string coercion.
 * answer is a trusted human-control capability: never expose it as a model tool.
 * Native guards still own path restrictions; every controlled `read` asks.
 * This module neither dispatches tools nor creates SDK agents or HTTP endpoints.
 */
export function installControls(ctx, { owner, publish, approvalTimeoutMs = 30_000, askTools = ['read'] }) {
  if (typeof owner !== 'function' || typeof publish !== 'function'
    || !Number.isFinite(approvalTimeoutMs) || approvalTimeoutMs <= 0
    || approvalTimeoutMs > 2_147_483_647) throw new TypeError('Invalid control options');
  const records = new Map(), pending = new Map();
  const forceAsk = new Set(askTools);
  let disposed = false;

  function ownership(agent) {
    try {
      if (!agent || ctx.agents.get(agent.id) !== agent) return undefined;
      const binding = snapshotBinding(owner(agent));
      return binding?.session_id === agent.id && agent.session.id === agent.id ? binding : undefined;
    } catch { return undefined; }
  }
  function closePending(state, outcome = 'cancelled') {
    for (const request of pending.values()) if (request.state === state) request.finish(outcome);
  }
  function retire(state) {
    state.revoked = true;
    closePending(state);
    state.reads.clear();
  }
  function live(state) {
    if (!disposed && !state.revoked && sameBinding(state.binding, ownership(state.agent))) return true;
    retire(state); return false;
  }
  function current(agent) {
    if (disposed) return undefined;
    const binding = ownership(agent), record = records.get(agent);
    if (!binding) { if (record) retire(record.state); return undefined; }
    if (record && sameBinding(record.state.binding, binding)) return live(record.state) ? record : undefined;
    if (record) retire(record.state);
    const state = { agent, binding, revoked: false, cancelling: false, reads: new Map(),
      sources: new Set(), ended: false };
    const next = { state, turns: new Map(), attempts: new Map() };
    records.set(agent, next); return next;
  }
  function source(state) {
    // Both producer names remain non-human even alongside role:user input.
    const kinds = state.sources;
    return { kind: kinds.has('project-reflection') ? 'project-reflection'
      : kinds.has('shikigami-reflection') ? 'shikigami-reflection'
      : kinds.has('shikigami-proactive') ? 'shikigami-proactive'
      : kinds.has('user') ? 'user' : kinds.values().next().value ?? 'unknown' };
  }
  function send(state, type, data, onFailure = () => {}) {
    if (!live(state)) return false;
    try {
      const result = publish({ ...state.binding, type, source: source(state), ...structuredClone(data) });
      // Promise-returning transports must not create unhandled rejections.
      if (result && typeof result.then === 'function') Promise.resolve(result).catch(onFailure);
      return true;
    } catch { onFailure(); return false; }
  }
  function turnFor(agent, turn) {
    const record = current(agent), entry = record?.turns.get(turn);
    return entry && !entry.ended && live(entry.state) ? entry : undefined;
  }

  ctx.on('agent/inbox/claimed', ({ agent, message, turn }) => {
    const record = current(agent);
    if (!record || record.state.ended) return;
    // Also supports installation before claiming an already-open native turn.
    if (!record.turns.has(turn)) record.turns.set(turn, { state: record.state, ended: false, seen: new Set() });
    const kind = message?.source?.kind;
    if (typeof kind === 'string' && kind) record.state.sources.add(kind);
  });
  ctx.on('agent/assistant-stream', ({ agent, frame }) => {
    const record = current(agent);
    if (!record || record.state.cancelling || record.state.ended) return;
    if (frame.type === 'start') {
      const turn = turnFor(agent, frame.turn);
      if (turn && !record.attempts.has(frame.attemptId)) record.attempts.set(frame.attemptId,
        { turn, turnNumber: frame.turn, step: frame.step, index: -1 });
      return;
    }
    const attempt = record.attempts.get(frame.attemptId);
    if (!attempt || attempt.turn.ended || !live(attempt.turn.state)) return;
    if (frame.type === 'end') { record.attempts.delete(frame.attemptId); return; }
    if (frame.type !== 'chunk' || !Number.isSafeInteger(frame.index) || frame.index <= attempt.index) return;
    attempt.index = frame.index;
    if (frame.chunk?.type === 'text-delta' && typeof frame.chunk.text === 'string') {
      send(attempt.turn.state, 'reply_delta', { text: frame.chunk.text,
        attempt_id: frame.attemptId, turn: attempt.turnNumber, step: attempt.step });
    }
  });
  ctx.on('agent/status', ({ agent, status }) => {
    const record = current(agent);
    if (record && (status === 'idle' || status === 'running')) send(record.state, 'status', { status });
  });
  ctx.on('session/event', (session, event) => {
    const agent = ctx.agents.get(session.id);
    if (!agent || agent.session !== session) return;
    const record = current(agent);
    if (!record) return;
    const data = event.data;
    if (event.type === 'turn/start') {
      if (!record.state.ended && !record.turns.has(data.turn)) {
        record.turns.set(data.turn, { state: record.state, ended: false, seen: new Set() });
      }
      return;
    }
    if (!['tool/call', 'tool/result', 'turn/end'].includes(event.type)) return;
    const turn = record.turns.get(data.turn);
    if (!turn || turn.ended || !live(turn.state)) return;
    const key = `${event.type}:${event.seq}`;
    if (turn.seen.has(key)) return;
    turn.seen.add(key);
    if (event.type === 'tool/call') {
      send(turn.state, 'tool_call', { turn: data.turn, step: data.step,
        call_id: data.callId, tool_name: data.name, arguments: data.arguments });
    } else if (event.type === 'tool/result') {
      const message = data.message;
      send(turn.state, 'tool_result', { turn: data.turn, step: data.step,
        call_id: message.source.callId, is_error: message.isError === true,
        content: message.content.filter(block => block.type === 'text')
          .map(block => ({ type: 'text', text: block.text })),
        ...(data.error === undefined ? {} : { error: data.error }) });
    } else {
      turn.ended = true; turn.state.ended = true;
      closePending(turn.state); turn.state.reads.clear(); record.attempts.clear();
      const type = data.reason?.kind === 'completed' ? 'completed'
        : data.reason?.kind === 'aborted' ? 'cancelled' : 'failed';
      send(turn.state, type, { turn: data.turn, reason: data.reason });
    }
  });

  ctx.on('tools/pre-execute', async (exec, next) => {
    const record = current(exec.agent), state = record?.state;
    if (!state || state.ended) return forceAsk.has(exec.name)
      ? { kind: 'deny', reason: 'Tool has no active control binding' } : next();
    if (state.cancelling || exec.signal.aborted) return { kind: 'cancel' };
    const decision = await next();
    if (!live(state) || state.cancelling || exec.signal.aborted) return { kind: 'cancel' };
    if (decision.kind === 'deny' || decision.kind === 'cancel') return decision;
    if (state.reads.has(exec.callId)) return { kind: 'deny', reason: 'Duplicate read call' };
    const args = structuredClone(exec.arguments);
    const reason = decision.reason ?? `Approve native ${exec.name}: ${JSON.stringify(args)}`;
    state.reads.set(exec.callId, { name: exec.name, args, reason, signal: exec.signal, claimed: false });
    // Returning ask delegates the sole request to the native registry/service.
    return forceAsk.has(exec.name) ? { kind: 'ask', reason } : decision;
  }, { prepend: true });

  ctx.on('approval/request', async (req, next) => {
    const state = current(req.agent)?.state, read = state?.reads.get(req.callId);
    if (!state) return forceAsk.has(req.toolName) ? 'unavailable' : next();
    if (!read || read.name !== req.toolName || read.claimed || state.ended) return 'unavailable';
    if (state.cancelling || req.signal?.aborted || read.signal.aborted) return 'cancelled';
    read.claimed = true;
    const id = randomUUID();
    return new Promise(resolve => {
      let done = false, timer;
      const signals = [...new Set([req.signal, read.signal].filter(Boolean))];
      const request = { state, finish(outcome) {
        if (done) return false;
        done = true; clearTimeout(timer); pending.delete(id);
        for (const signal of signals) signal.removeEventListener('abort', abort);
        resolve(outcome); return true;
      } };
      const abort = () => { request.finish('cancelled'); };
      pending.set(id, request);
      for (const signal of signals) signal.addEventListener('abort', abort, { once: true });
      timer = setTimeout(() => { request.finish('unavailable'); }, approvalTimeoutMs);
      if (signals.some(signal => signal.aborted) || !live(state)) { abort(); return; }
      send(state, 'approval_requested', { approval_id: id, tool_name: req.toolName,
        call_id: req.callId, reason: read.reason, action: req.toolName,
        path: typeof read.args?.file_path === 'string' ? read.args.file_path : null,
        arguments: read.args }, () => { request.finish('unavailable'); });
    });
  }, { prepend: true });

  function revoke(agent) {
    const record = records.get(agent);
    if (record) retire(record.state);
  }
  ctx.on('agent/disposed', ({ agent }) => { revoke(agent); records.delete(agent); });
  ctx.on('dispose', () => {
    disposed = true;
    for (const record of records.values()) retire(record.state);
    records.clear();
  });
  return {
    answer(binding, id, outcome) {
      const request = pending.get(id);
      if (!request || !sameBinding(request.state.binding, binding)) return false;
      if (!live(request.state) || request.state.cancelling || request.state.ended) return false;
      if (outcome !== 'allowed-once' && outcome !== 'rejected') return false;
      return request.finish(outcome);
    },
    async cancel(binding) {
      const valid = snapshotBinding(binding);
      if (!valid) return false;
      const agent = ctx.agents.get(valid.session_id), record = current(agent);
      if (!record || !sameBinding(record.state.binding, valid) || record.state.ended) return false;
      record.state.cancelling = true;
      closePending(record.state);
      send(record.state, 'status', { status: 'cancelling' });
      // Publishing invokes host code synchronously; it may revoke/rebind the run.
      if (!live(record.state) || record.state.ended) return false;
      agent.cancel({ kind: 'user' });
      await agent.whenIdle();
      return true; // acknowledgement of quiescence, never a synthetic terminal event
    },
    revoke,
  };
}
