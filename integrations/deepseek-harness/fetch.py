"""One native web fetch; localhost here is IPC, never destination retrieval."""
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

from deepseek_harness import DeepSeekHarness

MAX_REQUEST_BYTES = 8192
MAX_RESULT_BYTES = 2_000_000


def main():
    for package in ('deepseek-harness-sdk', 'deepseek-harness-runtime-bin'):
        if importlib.metadata.version(package) != '0.2.1rc2':
            raise RuntimeError('Unsupported native web runtime version')
    raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError('Request too large')
    request = json.loads(raw)
    if (not isinstance(request, dict) or set(request) != {'url'}
            or not isinstance(request['url'], str) or not 0 < len(request['url']) <= 4096):
        raise ValueError('Invalid web request')
    # Docker client proxy defaults must not let the native provider bypass its
    # direct DNS/public-address checks. This helper has no inherited host keys.
    for key in tuple(os.environ):
        if key.lower().endswith('_proxy'):
            os.environ.pop(key)
    patch = [
        {'id': 'web', 'config': {'fetchProvider': 'http'}},
        {'id': 'web-fetch-http', 'config': {'maxResponseBytes': 1_000_000,
            'maxBodyChars': 100_000, 'timeoutMs': 25000, 'maxRedirects': 3}},
        {'insert': [{'id': 'shikigami-fetch', 'name': '/opt/agent/fetch.mjs'}]},
    ]
    Path('/tmp/fetch.patch.json').write_text(json.dumps(patch), encoding='utf-8')
    # A fixed base URL avoids auto-endpoint discovery/polling at SDK startup.
    # No key, model preset, session prompt or model request is supplied.
    with DeepSeekHarness(dsh_home='/tmp/harness', cwd='/tmp', profile='sdk',
                         base_url='https://api.deepseek.com',
                         patches=('/tmp/fetch.patch.json',), initialize_timeout_seconds=20,
                         shutdown_timeout_seconds=3):
        req = urllib.request.Request('http://127.0.0.1:8094/fetch',
            data=json.dumps(request, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=30) as response:
                result = response.read(MAX_RESULT_BYTES + 1)
                if len(result) > MAX_RESULT_BYTES:
                    raise ValueError('Native result too large')
                print(json.dumps(json.loads(result), ensure_ascii=False), flush=True)
        except urllib.error.HTTPError as error:
            # Only a bounded machine code, never raw logs, URLs or headers.
            try:
                code = json.loads(error.read(1024)).get('code', '')
            except (ValueError, AttributeError):
                code = ''
            if isinstance(code, str) and re.fullmatch(r'WEB_[A-Z_]{1,64}', code):
                print(code, file=sys.stderr, flush=True)
            raise RuntimeError('Native webpage retrieval failed') from None


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Native webpage retrieval failed', file=sys.stderr, flush=True)
        sys.exit(1)
