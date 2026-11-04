"""Bound model egress. Credentials stay in the host process, never the worker."""
import json
from urllib.parse import urlsplit, urlunsplit

import httpx


def model_route(preset):
    base = str(preset.get("base_url") or "").strip().rstrip("/")
    model = str(preset.get("model") or "").strip()
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or not model:
        raise ValueError("Configure a model and an HTTP(S) endpoint in Models settings")
    if parsed.hostname == "generativelanguage.googleapis.com":
        return {"provider": "google", "model": model.removeprefix("models/"), "native_google": True,
                "target": "https://generativelanguage.googleapis.com/v1beta/models/" + model.removeprefix("models/") + ":streamGenerateContent?alt=sse"}
    path = parsed.path.rstrip("/")
    if path.lower().endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    return {"provider": "shikigami-model", "model": model, "native_google": False,
            "target": urlunsplit((parsed.scheme, parsed.netloc, path + "/chat/completions", parsed.query, ""))}


class ModelRequestLimitReached(RuntimeError):
    """A new model request was denied by the configured per-run limit."""


class ModelGateway:
    def __init__(self, preset, max_requests, *, authorize, client=None):
        self.route = model_route(preset)
        self._key = str(preset.get("api_key") or "")
        unsupported = set(preset.get('unsupported_params') or [])
        defaults = {'temperature': .9, 'top_p': .95, 'presence_penalty': .5, 'frequency_penalty': .4}
        self._params = {k: preset.get(k, value) for k, value in defaults.items()
                        if k not in unsupported and preset.get(k, value) is not None}
        self._extra = dict(preset.get('extra_body') or {})
        requested_tokens = preset.get('max_tokens')
        self._max_tokens = min(requested_tokens, 4096) if type(requested_tokens) is int and requested_tokens > 0 else 4096
        self.max_requests = max_requests
        self.requests = 0
        self.limit_reached = False
        self.authorize = authorize
        self.client = client

    async def relay(self, request, respond):
        """Forward one streaming request, without retries or endpoint selection by the worker."""
        self.authorize()
        if self.requests >= self.max_requests:
            self.limit_reached = True
            raise ModelRequestLimitReached("Model request limit reached")
        expected = ("/v1beta/models/" + self.route["model"] + ":streamGenerateContent"
                    if self.route["native_google"] else "/v1/chat/completions")
        if request.get("method") != "POST" or urlsplit(request.get("path", "")).path != expected:
            raise ValueError("Unapproved model endpoint")
        payload = request.get("body")
        if not isinstance(payload, dict) or len(json.dumps(payload)) > 8_000_000:
            raise ValueError("Invalid model request")
        self.requests += 1
        headers = {"Content-Type": "application/json"}
        if self.route["native_google"]:
            headers["x-goog-api-key"] = self._key
            generation = payload.setdefault("generationConfig", {})
            generation["maxOutputTokens"] = min(int(generation.get("maxOutputTokens") or 4096), self._max_tokens)
            for name in ("temperature", "topP", "topK"):
                generation.pop(name, None)
            thinking = generation.get("thinkingConfig")
            if isinstance(thinking, dict):
                thinking.pop("thinkingBudget", None)
                thinking["includeThoughts"] = False
        else:
            payload.update(self._params)
            for key, value in self._extra.items():
                if key not in ('messages', 'tools', 'model', 'stream', 'tool_choice', 'max_tokens', 'max_completion_tokens'):
                    payload[key] = value
            payload["model"] = self.route["model"]
            token_field = 'max_completion_tokens' if 'max_completion_tokens' in payload else 'max_tokens'
            payload[token_field] = min(int(payload.get(token_field) or 4096), self._max_tokens)
            payload.pop('max_tokens' if token_field == 'max_completion_tokens' else 'max_completion_tokens', None)
            if self._key:
                headers["Authorization"] = "Bearer " + self._key
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15), follow_redirects=False)
        started = False
        try:
            async with client.stream("POST", self.route["target"], headers=headers, json=payload) as response:
                await respond("start", status=response.status_code, content_type=response.headers.get("content-type", "application/json"))
                started = True
                if not 200 <= response.status_code < 300:
                    # Error bodies may echo Authorization or query credentials.
                    await respond("chunk", data=json.dumps({"error": {"message": f"Model provider returned HTTP {response.status_code}"}}).encode())
                    await respond("end")
                    return
                pending = b''
                secret = self._key.encode() if self._key else b''
                async for chunk in response.aiter_bytes():
                    self.authorize()
                    pending += chunk
                    if secret:
                        pending = pending.replace(secret, b'[redacted]')
                    keep = len(secret) - 1 if secret else 0
                    if len(pending) > keep:
                        count = len(pending) - keep
                        await respond("chunk", data=pending[:count])
                        pending = pending[count:]
                if pending:
                    await respond("chunk", data=pending)
                await respond("end")
        except Exception:
            # Never propagate SDK/httpx exceptions with credential-bearing URLs or headers.
            if not started:
                await respond("start", status=502, content_type="application/json")
                await respond("chunk", data=b'{"error":{"message":"Model request failed; no automatic retry"}}')
            await respond("end")
            raise RuntimeError("Model request failed or was cancelled") from None
        finally:
            if self.client is None:
                await client.aclose()
