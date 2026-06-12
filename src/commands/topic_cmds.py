"""topic_cmds.py — /topic 主动找话题命令 + 自然语言触发共用的选材逻辑。

零额外 LLM 调用：候选收集 / 挑选 / 素材解析全是确定性代码，
结果作为 context_for_llm 注入本次回复的 prompt，由 AI 在同一次生成里自然引出话题。

挑选偏好：外部来源（网络趋势 / 随机话题）优先——手动要新话题的人想听新鲜事；
外部无货时回落到用户生活 / 对话回忆 / AI 自身。
"""
import logging
import random
from typing import Optional

from src.commands.base import CommandResult
from src.config.prompt_loader import get_locale

logger = logging.getLogger(__name__)

_EXTERNAL_SOURCES = ["trend", "random_api"]
_FALLBACK_SOURCES = ["user_life", "conversation_recall", "ai_self"]

# /topic <sub> 指定来源
_SUB_TO_SOURCE = {
    "trend": "trend", "趋势": "trend",
    "recall": "conversation_recall", "回忆": "conversation_recall",
    "life": "user_life", "生活": "user_life",
    "self": "ai_self", "自己": "ai_self",
    "random": "random_api", "随机": "random_api",
}


def _build_tctx(session, app, td_cfg):
    """构造 TopicSourceContext（与 ase._resolve_chosen_topic 同构）。"""
    import json
    import os
    from src.core.topics.base import TopicSourceContext
    from src.config.effective_config import _load_profile_card
    from src.prompt.segments.reflection.topic_candidates import _recently_used_refs

    profile = _load_profile_card(session.profile_id)
    emotion = None
    try:
        epath = os.path.join(session.storage_root, "emotion_state.json")
        if os.path.exists(epath):
            with open(epath, encoding="utf-8") as f:
                emotion = json.load(f)
    except Exception:
        pass
    mm = None
    try:
        mm = getattr(app.state, "memory_managers", {}).get(session.profile_id)
    except Exception:
        pass
    rs = getattr(session, "reflection_state", None) or {}
    window = int(td_cfg.get("recent_used_window", 10))

    return TopicSourceContext(
        profile_id=session.profile_id,
        storage_root=session.storage_root,
        profile=profile,
        config=td_cfg,
        recently_used=_recently_used_refs(session.storage_root, window),
        reflection_thought=(rs.get("thought") or ""),
        emotion=emotion,
        memory_manager=mm,
        session=session,
        app=app,
    )


def pick_topic_material(session, app, want_source: str = "") -> Optional[dict]:
    """收集候选 → 按偏好挑一条 → resolve 素材 → mark_used。纯确定性，无 LLM 调用。

    返回 {source_id, source_label, material, framing_hint}；无可用素材返回 None。
    """
    from src.core.topics import get_enabled_sources
    try:
        from src.config.effective_config import get_effective_topic_discovery_config
        td_cfg = get_effective_topic_discovery_config(app, session.profile_id)
    except Exception:
        td_cfg = {}

    sources = {s.source_id: s for s in get_enabled_sources(td_cfg)}
    if not sources:
        return None
    tctx = _build_tctx(session, app, td_cfg)

    if want_source:
        order = [want_source]
    else:
        order = _EXTERNAL_SOURCES + _FALLBACK_SOURCES
    # 外部来源内部随机化，避免每次都同一来源
    locale = get_locale()

    for sid in order:
        src = sources.get(sid)
        if src is None:
            continue
        try:
            candidates = list(src.get_candidates(tctx) or [])
        except Exception as e:
            logger.debug("[topic_cmds] 来源 %s 取候选失败: %s", sid, e)
            continue
        random.shuffle(candidates)
        for cand in candidates:
            try:
                material = src.resolve(cand.item_id, tctx)
            except Exception as e:
                logger.debug("[topic_cmds] resolve 失败 (%s:%s): %s", sid, cand.item_id, e)
                continue
            if material is None:
                continue
            try:
                src.mark_used(cand.item_id, tctx)
            except Exception:
                pass
            label = getattr(src, "label_en" if locale == "en" else "label", sid)
            return {
                "source_id": sid,
                "source_label": label,
                "material": material.material,
                "framing_hint": src.get_framing_hint(locale),
            }
    return None


def _format_context(picked: Optional[dict], trigger_text: str) -> str:
    """把选材结果格式化为注入本次回复的 system 上下文。"""
    locale = get_locale()
    if picked is None:
        if locale == "en":
            return (f"[Command result · {trigger_text}]\n"
                    "The user asked you to bring up a new topic, but no fresh material is "
                    "available right now (trends may be disabled, still fetching, or used up). "
                    "Acknowledge that honestly, then start a topic of your own from your "
                    "shared memories or your current state — don't pretend you found news.")
        return (f"[命令执行结果 · {trigger_text}]\n"
                "用户想让你起一个新话题，但素材库此刻没有新鲜内容（趋势可能未启用、"
                "还没抓到或都聊过了）。请坦然说明，然后从你们的回忆或你自己的近况里"
                "主动起一个话题——不要假装看到了新消息。")
    if locale == "en":
        return (f"[Command result · {trigger_text}]\n"
                f"The user asked you to bring up a new topic. Material picked for you "
                f"(source: {picked['source_label']}):\n{picked['material']}\n\n"
                f"How to frame it: {picked['framing_hint']}\n"
                f"Respond briefly to the request first, then lead into the topic naturally, "
                f"in your own voice.")
    return (f"[命令执行结果 · {trigger_text}]\n"
            f"用户想让你起一个新话题。为你选好的素材（来源：{picked['source_label']}）：\n"
            f"{picked['material']}\n\n"
            f"切入方式：{picked['framing_hint']}\n"
            f"先简短回应用户的请求，再用你自己的口吻自然引出这个话题，不要念稿。")


async def handle_topic(sub: str, args: str, session, app) -> CommandResult:
    """/topic [trend|回忆|生活|自己|随机] — 让 AI 立刻主动起一个新话题。"""
    want_source = _SUB_TO_SOURCE.get((sub or "").strip().lower(), "")
    picked = pick_topic_material(session, app, want_source=want_source)
    cmd_text = f"/topic {sub}".strip()
    return CommandResult(context_for_llm=_format_context(picked, cmd_text))
