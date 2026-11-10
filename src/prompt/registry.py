"""Global segment registry.

Usage
-----
# In a segment module:
from src.prompt.registry import register
from src.prompt.base import PromptSegment

@register
class MySegment(PromptSegment):
    segment_id = "my_segment"
    priority   = 30
    ...

# In the pipeline:
from src.prompt.registry import get_registered
segments = get_registered()   # sorted by priority
"""
from __future__ import annotations

import logging
from typing import Dict, List, Type

from src.prompt.base import PromptSegment

logger = logging.getLogger(__name__)

_registry: Dict[str, Type[PromptSegment]] = {}


def register(cls: Type[PromptSegment]) -> Type[PromptSegment]:
    """Class decorator — registers a PromptSegment subclass."""
    if not cls.segment_id:
        raise ValueError(f"PromptSegment subclass {cls.__name__} must define segment_id")
    if cls.segment_id in _registry:
        logger.warning(f"[Registry] segment_id '{cls.segment_id}' already registered, overwriting")
    _registry[cls.segment_id] = cls
    logger.debug(f"[Registry] registered segment '{cls.segment_id}' (priority={cls.priority})")
    return cls


def get_registered() -> List[Type[PromptSegment]]:
    """Return all registered segment classes sorted by priority (ascending)."""
    return sorted(_registry.values(), key=lambda c: c.priority)


def get_segment_class(segment_id: str) -> Type[PromptSegment] | None:
    """Look up a segment class by ID."""
    return _registry.get(segment_id)


def all_segment_ids() -> List[str]:
    return list(_registry.keys())


_discovery_done = False


def discover_segments() -> None:
    """Import every module under ``src.prompt.segments`` (including the ``ase/``
    and ``reflection/`` subpackages) so all ``@register`` decorators run.

    Idempotent. Called once at ``src.prompt.pipeline`` import time, so any
    caller that imports the pipeline — server, script or test — gets the full
    segment inventory, including reflection-only segments. Tool-package
    segments (``src/tools/*/segment.py``) are not covered here; they register
    through their tool's ``register(app)`` and stay inert without the tool.
    """
    global _discovery_done
    if _discovery_done:
        return
    _discovery_done = True
    import importlib
    import pkgutil

    import src.prompt.segments as _pkg
    for info in pkgutil.walk_packages(_pkg.__path__, prefix="src.prompt.segments."):
        try:
            importlib.import_module(info.name)
        except Exception:
            logger.exception("[Registry] segment module %s failed to import", info.name)
