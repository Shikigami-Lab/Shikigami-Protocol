// Local IPC only: public retrieval and DNS/redirect policy belong to ctx.web.
import http from 'node:http';
export const name = 'shikigami-native-fetch';
export const inject = ['web'];
export function apply(ctx) {
  let used = false;
  const active = new Set();
  const server = http.createServer(async (req, res) => {
    if (req.method !== 'POST' || req.url !== '/fetch') { res.writeHead(404).end(); return; }
    if (used) { res.writeHead(409).end(); return; }
    used = true; // One attempt per helper, including failed attempts.
    const controller = new AbortController();
    active.add(controller);
    const timer = setTimeout(() => controller.abort(), 25000);
    const disconnect = () => { if (!res.writableFinished) controller.abort(); };
    res.on('close', disconnect);
    try {
      const chunks = [];
      let bytes = 0;
      for await (const part of req) {
        bytes += part.length;
        if (bytes > 8192) throw new Error('Request too large');
        chunks.push(part);
      }
      const request = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (!request || Array.isArray(request) || Object.keys(request).length !== 1
          || typeof request.url !== 'string' || !request.url.length || request.url.length > 4096) {
        throw new Error('Invalid request');
      }
      const result = await ctx.web.fetch(request, controller.signal);
      const encoded = JSON.stringify(result);
      if (Buffer.byteLength(encoded) > 2_000_000) throw new Error('Result too large');
      res.setHeader('Content-Type', 'application/json');
      res.end(encoded);
    } catch (error) {
      const code = typeof error?.code === 'string' && /^WEB_[A-Z_]{1,64}$/.test(error.code)
        ? error.code : 'WEB_PROVIDER_ERROR';
      res.writeHead(502, {'Content-Type': 'application/json'}).end(JSON.stringify({code}));
    } finally {
      clearTimeout(timer);
      res.off('close', disconnect);
      active.delete(controller);
    }
  });
  server.requestTimeout = 30000;
  server.headersTimeout = 5000;
  server.listen(8094, '127.0.0.1');
  ctx.on('dispose', () => new Promise(resolve => {
    for (const controller of active) controller.abort();
    server.close(resolve);
    server.closeAllConnections();
  }));
}
