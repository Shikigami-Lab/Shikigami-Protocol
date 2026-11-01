"""Small UI projections, not copies of native transcripts or file bodies."""
import hashlib
import json
import re
from src.agent.link_store import _FORBIDDEN_KEYS, _THINKING_KEYS, _SECRET


def safe_text(value, limit=900):
    return _SECRET.sub('[redacted]', str(value))[:limit]


def project_arguments(arguments):
    def clean(value, depth=0):
        if depth > 5:
            return '[nested value omitted]'
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:40]:
                normalized = re.sub(r'[^a-z0-9]', '', str(key).lower())
                if normalized == 'content' and isinstance(item, str):
                    result['content_bytes'] = len(item.encode())
                    result['content_sha256'] = hashlib.sha256(item.encode()).hexdigest()
                elif normalized not in _FORBIDDEN_KEYS | _THINKING_KEYS:
                    result[str(key)[:100]] = clean(item, depth + 1)
            return result
        if isinstance(value, list):
            return [clean(item, depth + 1) for item in value[:20]]
        if isinstance(value, str):
            return safe_text(value, 2000)
        if value is None or type(value) in (bool, int, float):
            return value
        return '[unsupported value]'
    result = clean(arguments)
    encoded = json.dumps(result, ensure_ascii=False).encode()
    if len(encoded) > 4000:
        return {'arguments_omitted': True, 'arguments_bytes': len(encoded),
                'arguments_sha256': hashlib.sha256(encoded).hexdigest()}
    return result


def artifact_preview(files):
    """Keep whole paths and reserve space for the rest of the 8KB run event."""
    result = []
    for file in files[:20]:
        # Filenames may resemble credentials. Leave such files reachable in
        # the scoped workspace inventory, without breaking the receipt commit.
        if _SECRET.search(str(file.get('path', ''))):
            continue
        candidate = result + [file]
        if len(json.dumps(candidate, ensure_ascii=False).encode()) > 4000:
            break
        result = candidate
    return result, len(result) < len(files)
