import { randomUUID } from 'node:crypto';

const MAX_MESSAGES = 4096;
const MAX_TEXT_BYTES = 4 * 1024 * 1024;
const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);

/**
 * Convert a schema-1 Python-normalized text envelope to native rc2 seed events.
 * The final user text is returned separately as trigger; enqueue it exactly once.
 * Fresh system/user input yields an empty seed; native assembly creates its
 * system message. The caller configures the original envelope's system prefix
 * separately for live assembly. With history, system is the first surface node
 * inside the first historical turn/step (both 1), with no empty bootstrap turn.
 * Native V4 persistence requires positive system coordinates; ordinary Session
 * seed acceptance alone does not establish that storage contract.
 *
 * Seeded system text (including literal braces and its rendered chat-time
 * index) is preserved exactly, with no model-facing import notice. All native
 * event times are `now` in integer milliseconds: import time, NOT reconstructed
 * historical chat times. This distinction is metadata, not prompt content.
 *
 * Historical provider/model are absent from this text-only envelope. The names
 * below are a RESERVED LOCAL MAPPING for unknown attribution, not an upstream
 * sentinel or a claim that these models exist. Never substitute the active
 * provider, fabricate usage/signatures/replay state, or replay old model calls.
 * Empty assistant streams are supported by upstream migration and seed tests.
 *
 * Bounds: 1..256 Unicode code points per opaque ID, <=4096 messages and <=4 MiB
 * total UTF-8 input text. Message records contain only role/content; unrelated
 * envelope transport fields are ignored. This does not authenticate the caller.
 * No I/O, input mutation, model calls or SDK dependency. Throws TypeError for
 * invalid/unbounded input. UUIDs are new native identities on every import.
 */
export function buildHistorySeed(envelope, { now = Date.now() } = {}) {
  if (!isRecord(envelope) || envelope.schema_version !== 1) {
    throw new TypeError('schema_version must be 1 in a context envelope');
  }
  for (const key of ['profile_id', 'request_id']) {
    const id = envelope[key];
    if (typeof id !== 'string' || !id.trim() || [...id].length > 256) {
      throw new TypeError(`${key} must be a nonblank string of at most 256 characters`);
    }
  }
  if (!Number.isSafeInteger(envelope.context_revision) || envelope.context_revision < 0) {
    throw new TypeError('context_revision must be a nonnegative safe integer');
  }
  if (!['user', 'reflection'].includes(envelope.origin)) {
    throw new TypeError('origin must be user or reflection');
  }
  if (!Number.isSafeInteger(now) || now < 0 || Object.is(now, -0)) {
    throw new TypeError('now must be a nonnegative safe integer import timestamp');
  }
  const { messages } = envelope;
  if (!Array.isArray(messages) || messages.length < 2 || messages.length > MAX_MESSAGES) {
    throw new TypeError('messages must contain 2..4096 records (message count bound)');
  }
  let bytes = 0;
  for (let index = 0; index < messages.length; index++) {
    const item = messages[index];
    const expectedRole = index === 0 ? 'system' : index % 2 === 1 ? 'user' : 'assistant';
    if (!isRecord(item) || Object.keys(item).length !== 2
        || !Object.hasOwn(item, 'role') || !Object.hasOwn(item, 'content')
        || item.role !== expectedRole || typeof item.content !== 'string') {
      throw new TypeError(`messages[${index}] must contain only text role/content, with role ${expectedRole}`);
    }
    bytes += Buffer.byteLength(item.content, 'utf8');
    if (bytes > MAX_TEXT_BYTES) throw new TypeError('messages exceed the 4 MiB text bound');
  }
  const current = messages.at(-1);
  if (current.role !== 'user' || !current.content.trim()) {
    throw new TypeError('final trigger must be a nonblank user message');
  }

  const seed = [];
  const append = (type, data, surface = false) => {
    seed.push({ type, seq: seed.length, time: now, data, ...(surface ? { surfaceOp: 'append' } : {}) });
  };
  const nativeMessage = (role, text, source) => ({
    id: randomUUID(), role, content: [{ type: 'text', text }], source,
  });
  for (let index = 1, turn = 1; index < messages.length - 1; index += 2, turn++) {
    append('turn/start', { turn });
    append('step/start', { turn, step: 1 });
    if (turn === 1) {
      append('system/message', { turn, step: 1,
        message: nativeMessage('system', messages[0].content, { kind: 'system-prompt' }) }, true);
    }
    append('user/message', nativeMessage('user', messages[index].content,
      { kind: 'shikigami-history', form: 'recall' }), true);
    append('assistant/message', { turn, step: 1,
      message: nativeMessage('assistant', messages[index + 1].content,
        { kind: 'model', provider: 'shikigami-history/unknown-provider', model: 'unknown-model' }),
      stream: [] }, true);
    append('step/end', { turn, step: 1 });
    append('turn/end', { turn, reason: { kind: 'completed' } });
  }
  return { seed, trigger: current.content };
}
