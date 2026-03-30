"""Lorebook Segment — priority=81

从人格绑定的世界书文件（lorebooks/<id>.json）或人格内嵌 lorebook 读取条目：
  - 优先 profile["lorebook_ref"] 指向的文件
  - 文件缺失时回退到 profile["lorebook"]（旧数据）
  - scan_turns 来自世界书文件或内嵌 lorebook_settings

关键词触发 + 常驻（constant）逻辑见 resolve_lorebook_for_profile。
"""
import logging

from src.lorebooks.entry_utils import entry_is_constant
from src.lorebooks.store import resolve_lorebook_for_profile
from src.prompt.base import BuildContext, PromptSegment, SegmentResult
from src.prompt.registry import register

logger = logging.getLogger(__name__)

_DEFAULT_SCAN_TURNS = 10
_MIN_SCAN = 1
_MAX_SCAN = 50


def _scan_turns_from_settings(settings: dict) -> int:
    try:
        n = int((settings or {}).get("scan_turns", _DEFAULT_SCAN_TURNS))
        return max(_MIN_SCAN, min(_MAX_SCAN, n))
    except (TypeError, ValueError):
        return _DEFAULT_SCAN_TURNS


def _build_scan_text(ctx: BuildContext, n_turns: int) -> str:
    """拼接扫描窗口：当前用户消息 + 最近 n_turns 条对话内容。"""
    parts = [ctx.user_msg or ""]
    try:
        recent = ctx.store.get_recent(n_turns)
        parts.extend(turn.get("content", "") for turn in recent)
    except Exception:
        pass
    return " ".join(parts)


@register
class LoreBookSegment(PromptSegment):
    segment_id = "lorebook"
    priority = 81
    label = "世界书"
    description = "关键词或常驻条目注入设定；在「世界书」页编辑文件或绑定 lorebooks/ 中的书"
    is_core = False
    default_trigger_mode = "always"

    def build(self, ctx: BuildContext) -> SegmentResult:
        entries, lb_settings = resolve_lorebook_for_profile(ctx.profile or {})
        if not entries:
            return SegmentResult(fired=False)

        scan_n = _scan_turns_from_settings(lb_settings)
        scan_text = _build_scan_text(ctx, scan_n).lower()
        scan_nonempty = bool(scan_text.strip())

        matched = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if not entry.get("enabled", True):
                continue
            content = (entry.get("content") or "").strip()
            if not content:
                continue
            if entry_is_constant(entry, lb_settings):
                matched.append(content)
                continue
            keys = entry.get("keys") or []
            if not keys:
                continue
            if not scan_nonempty:
                continue
            if any(str(k).lower() in scan_text for k in keys):
                matched.append(content)

        if not matched:
            return SegmentResult(fired=False)

        block = "\n\n".join(matched)
        text = f"[世界设定]\n{block}"
        # SegmentResult 仅支持 messages / fired；传入其他字段会在 pipeline 里抛 TypeError 并整段跳过
        logger.debug(
            "[lorebook] 命中 %d 条（共 %d 条）scan_turns=%d",
            len(matched),
            len(entries),
            scan_n,
        )

        return SegmentResult(messages=[{"role": "system", "content": text}])
