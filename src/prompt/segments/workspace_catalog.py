"""Workspace catalog segment — the character's file directory in context.

Replaces the ad-hoc system-message append in ``AgentRuntime.prepare()`` for
chat/reflection turns: the catalog (bounded to 12 entries / 6,000 characters
by ``src/agent/workspace_catalog.py``) is now an ordinary segment with a UI
toggle and a dependency lock on the workspace capability.

The segment fires only when the caller opts in via
``extras["include_workspace_catalog"]`` — the chat endpoint sets it on
tools-allowed turns and the autonomy runner sets it for reflection-origin
runs.  Proactive research runs bypass the chat pipeline entirely and still
receive the catalog from ``AgentRuntime.prepare()``.
"""
from __future__ import annotations

import logging

from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)


@register
class WorkspaceCatalogSegment(PromptSegment):
    segment_id = "workspace_catalog"
    label = "工作区目录"
    description = ("注入角色工作区的文件目录（上限 12 条 / 6000 字符），"
                   "让模型知道自己有哪些文件")
    priority = 86
    is_readonly = True
    requires = ("capability:workspace",)

    def build(self, ctx: BuildContext) -> SegmentResult:
        if not ctx.extras.get("include_workspace_catalog"):
            return SegmentResult(messages=[], fired=False)
        app = ctx.extras.get("app")
        runtime = getattr(getattr(app, "state", None), "agent_runtime", None)
        if runtime is None:
            return SegmentResult(messages=[], fired=False)
        try:
            text = runtime.workspace_context(ctx.session.profile_id)
        except Exception as e:
            logger.warning("[WorkspaceCatalogSegment] catalog failed: %s", e)
            return SegmentResult(messages=[], fired=False)
        if not text:
            return SegmentResult(messages=[], fired=False)
        return SegmentResult(messages=[{"role": "system", "content": text}])
