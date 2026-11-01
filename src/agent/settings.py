"""Agent settings use the existing application YAML and model presets."""
from pathlib import Path
import os
import tempfile

DEFAULTS = {"enabled": False, "max_model_requests": 6, "timeout_seconds": 120}
DEFAULTS.update(autonomy_enabled=False, autonomy_interval_seconds=900, autonomy_max_requests_per_hour=12)
DEFAULTS.update({key: False for key in ('workspace_enabled', 'code_enabled', 'web_enabled', 'history_enabled')})


def normalize_settings(raw=None):
    value = {**DEFAULTS, **(raw or {})}
    for key in ('enabled', 'workspace_enabled', 'code_enabled', 'web_enabled', 'history_enabled', 'autonomy_enabled'):
        if type(value[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
    if value['code_enabled'] and not value['workspace_enabled']:
        raise ValueError('Code execution requires the workspace')
    for name, minimum, maximum in (("max_model_requests", 1, 32), ("timeout_seconds", 10, 600),
            ('autonomy_interval_seconds', 60, 86400), ('autonomy_max_requests_per_hour', 1, 128)):
        if type(value[name]) is not int or not minimum <= value[name] <= maximum:
            raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return {key: value[key] for key in DEFAULTS}


def save_settings(path, settings):
    """Preserve unrelated YAML fields/comments and replace atomically."""
    from ruamel.yaml import YAML
    destination = Path(path)
    yaml = YAML()
    yaml.preserve_quotes = True
    data = yaml.load(destination.read_text(encoding="utf-8")) if destination.exists() else {}
    data = data or {}
    data["agent"] = normalize_settings(settings)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".agent-settings-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
