"""reflection.py — ReflectionEngine: independent inner-monologue engine

Runs a background asyncio loop that periodically calls a lightweight LLM
to produce a per-session "inner state":
    - thought: what the AI is currently thinking about
    - urgency: 0.0–1.0 indicating desire to speak proactively
    - topic_hint: brief label for the current context

Key design principles:
    - Completely independent of ASE: reflection continues even if ASE is disabled
    - Uses real ConversationStore history (not vector fragments)
    - Writes to session.reflection_state and persists via session.save_runtime_state() (runtime_state.json)
    - Primary + fallback LLM model support
    - All writes are best-effort — exceptions never crash the loop

The reflection state is consumed by:
    1. ReflectionStateSegment — injected into every normal chat (if enabled)
    2. AseEngine — reads urgency to decide whether to speak proactively
"""
import asyncio
import json
import logging
import os
import time
import traceback
from typing import Any, Dict, Optional

from src.config.prompt_loader import get_prompt, get_locale, render
from src.utils.debug_logger import log_error
from src.utils.paths import get_project_root
from src.utils.persona_context import get_persona_context_for_secondary_llm
from src.prompt.base import ReflectionBuildContext, targets_reflection
from src.prompt.segment_config import effective_segment_enabled, load_segment_config

# Import reflection segments to trigger @register
import src.prompt.segments.reflection.persona        # noqa: F401
import src.prompt.segments.reflection.user_persona   # noqa: F401
import src.prompt.segments.reflection.ase_config     # noqa: F401
import src.prompt.segments.reflection.prev_thought   # noqa: F401
import src.prompt.segments.reflection.emotion        # noqa: F401
import src.prompt.segments.reflection.affinity       # noqa: F401
import src.prompt.segments.reflection.silence        # noqa: F401
import src.prompt.segments.reflection.ase_context     # noqa: F401
import src.prompt.segments.reflection.trend_context   # noqa: F401
import src.prompt.segments.reflection.proactive_log   # noqa: F401
import src.prompt.segments.reflection.memory_facts    # noqa: F401
import src.prompt.segments.reflection.user_engagement # noqa: F401
import src.prompt.segments.reflection.recent_dialogue # noqa: F401

logger = logging.getLogger(__name__)


def _reflection_user_instruction(locale: str = None) -> str:
    if locale is None:
        locale = get_locale()
    return get_prompt("reflection.user_instruction", locale=locale,
                      default=(
                          "What are you thinking right now? Feel first, then fill in the JSON. "
                          "Output only one JSON object with exactly these five English keys: "
                          "thought, style_hint, urgency, topic_hint, next_reflection_in. Nothing else, no non-English key names."
                          if locale == "en"
                          else "此刻你在想什么？先感受，再填入 JSON。仅输出一个 JSON 对象，必须只包含这五个英文键：thought, style_hint, urgency, topic_hint, next_reflection_in。不要其他内容、不要中文键名。"
                      ))


# 自省 LLM 单次生成 token 上限（过小会导致模型截断、解析失败；部分模型会先输出 markdown 再 JSON，需留余量）
REFLECTION_MAX_TOKENS = 2048

# ── Reflection LLM prompt ────────────────────────────────────────────────────

def _build_system_prompt(
    persona_name: str = "AI",
    persona_brief: str = "",
    context_notes: list = None,
    ase_decision_context: Optional[Dict[str, Any]] = None,
    locale: str = None,
) -> str:
    """Build a persona-aware system prompt for the reflection LLM."""
    if locale is None:
        locale = get_locale()

    persona_section = ""
    if persona_brief:
        persona_section = render("reflection.persona_section", locale=locale,
                                 persona_name=persona_name, persona_brief=persona_brief)

    notes_section = ""
    if context_notes:
        notes_text = "\n".join(f"- {n}" for n in context_notes)
        notes_section = render("reflection.notes_section", locale=locale, notes_text=notes_text)

    ase_section = ""
    if ase_decision_context:
        mode_name = ase_decision_context.get("mode_name", "medium")
        threshold = ase_decision_context.get("urgency_threshold", 0.35)
        ase_section = render("reflection.ase_section", locale=locale,
                             mode_name=mode_name, threshold=threshold)

    return render("reflection.system_base", locale=locale,
                  persona_name=persona_name,
                  persona_section=persona_section,
                  notes_section=notes_section,
                  ase_section=ase_section)


def _format_silence_tier(silent_seconds: float, locale: str = None) -> str:
    """用户沉默时长：5分钟内=才说过话，之后按分钟、小时、天递进。"""
    if locale is None:
        locale = get_locale()
    if silent_seconds < 300:
        return get_prompt("reflection.silence_tier.just_spoke", locale=locale,
                          default=("user just spoke within 5 minutes" if locale == "en" else "5分钟之内，用户才说过话"))
    if silent_seconds < 3600:
        return render("reflection.silence_tier.minutes", locale=locale,
                      default=("user has been silent for $minutes minutes" if locale == "en" else "用户已沉默 $minutes 分钟"),
                      minutes=int(silent_seconds / 60))
    if silent_seconds < 86400:
        return render("reflection.silence_tier.hours", locale=locale,
                      default=("user has been silent for $hours hours" if locale == "en" else "用户已沉默 $hours 小时"),
                      hours=int(silent_seconds / 3600))
    return render("reflection.silence_tier.days", locale=locale,
                  default=("user has been silent for $days days" if locale == "en" else "用户已沉默 $days 天"),
                  days=int(silent_seconds / 86400))


def build_reflection_messages(profile_id: str, ctx: ReflectionBuildContext) -> list:
    """Assemble reflection prompt from registered reflection segments + custom segments."""
    from src.prompt.registry import get_registered

    seg_cfg = load_segment_config(profile_id)

    # Gather all registered reflection segments
    all_segs = [cls for cls in get_registered() if targets_reflection(cls.inject_into)]

    def _effective_priority(cls):
        meta = seg_cfg.get_meta(cls.segment_id)
        if meta and meta.priority is not None:
            return meta.priority
        return cls.priority

    # Prepend the base framing: role declaration, JSON format, thought writing rules, urgency table.
    # persona_brief and ase_section are omitted here — dedicated segments handle them below.
    parts = [_build_system_prompt(
        persona_name=ctx.persona_name or "AI",
        persona_brief="",
        context_notes=ctx.context_notes or [],
        ase_decision_context=None,
        locale=ctx.locale,
    )]

    for seg_cls in sorted(all_segs, key=_effective_priority):
        meta = seg_cfg.get_meta(seg_cls.segment_id)
        enabled = effective_segment_enabled(meta, seg_cls)
        if not enabled and not seg_cls.is_core:
            continue
        try:
            instance = seg_cls()
            result = instance.build(ctx)
            if result and result.messages:
                for m in result.messages:
                    text = (m.get("content") or "").strip()
                    if text:
                        parts.append(text)
        except Exception as e:
            import logging as _logging
            _logging.getLogger(__name__).debug("[reflection] segment %s build failed: %s", seg_cls.segment_id, e)

    # Custom segments targeting reflection
    for c in seg_cfg.custom_segments:
        if targets_reflection(c.inject_into) and c.enabled and (c.content or "").strip():
            parts.append(c.content.strip())

    system_content = "\n\n".join(p for p in parts if p)
    locale = ctx.locale or "zh"
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": _reflection_user_instruction(locale)},
    ]


def _build_reflection_prompt(
    recent_turns: list,
    emotion: Optional[Dict] = None,
    affinity: Optional[Dict] = None,
    silent_seconds: float = 0,
    persona_name: str = "AI",
    persona_brief: str = "",
    ase_context: Optional[Dict[str, Any]] = None,
    prev_reflection: Optional[Dict[str, Any]] = None,
    context_notes: Optional[list] = None,
    ase_decision_context: Optional[Dict[str, Any]] = None,
    last_user_message_time: Optional[float] = None,
    user_name: str = "用户",
) -> list:
    """Build messages for reflection: 单块 system = 内置规则(persona_brief/urgency 等) + 最近对话 + 当前上下文。
    persona_brief 与情感分类、好感度 LLM 同源：reflection_config.custom_prompt（截断见 get_persona_context_for_secondary_llm），不含 base_prompt。
    """
    locale = get_locale()
    user_label = "User" if locale == "en" else "用户"
    ctx_parts = []

    # 【你上一轮自省】便于连贯与校准 urgency，减少无依据的跳变
    if prev_reflection:
        urgency = prev_reflection.get("urgency")
        topic = (prev_reflection.get("topic_hint") or "").strip()
        updated_at = prev_reflection.get("updated_at") or 0
        if topic or urgency is not None:
            line = get_prompt("reflection.prev_reflection_header", locale=locale,
                              default=("[Your Last Reflection]" if locale == "en" else "【你上一轮自省】"))
            if topic:
                line += render("reflection.prev_topic", locale=locale,
                               default=("Your last noted topic was: $topic" if locale == "en" else "你上次在意的话题是：$topic"), topic=topic)
            if urgency is not None:
                line += render("reflection.prev_urgency", locale=locale,
                               default=("; urgency was $urgency" if locale == "en" else "；urgency 为 $urgency"), urgency=round(urgency, 1))
            if updated_at > 0:
                elapsed = time.time() - updated_at
                if elapsed < 60:
                    time_ago = render("reflection.silence_tier.just_spoke", locale=locale,
                                      default=("less than 1 minute" if locale == "en" else "不到1分钟"))
                elif elapsed < 3600:
                    time_ago = render("time_context.human_delta.minutes", locale=locale,
                                      default=("$minutes minutes" if locale == "en" else "$minutes 分钟"), minutes=int(elapsed / 60))
                elif elapsed < 86400:
                    time_ago = render("time_context.human_delta.hours", locale=locale,
                                      default=("$hours hours" if locale == "en" else "$hours 小时"), hours=int(elapsed / 3600))
                else:
                    time_ago = render("time_context.human_delta.days", locale=locale,
                                      default=("$days days" if locale == "en" else "$days 天"), days=int(elapsed / 86400))
                line += render("reflection.prev_elapsed", locale=locale,
                               default=("; $time_ago has passed since your last reflection" if locale == "en" else "；距离上次自省已经过去 $time_ago"), time_ago=time_ago)
            ctx_parts.append(line)

    # Emotion state
    if emotion:
        layers = emotion.get("emotion_layers") or []
        emotion_header = get_prompt("reflection.emotion_label", locale=locale,
                                    default=("[Current Emotion]" if locale == "en" else "【当前情感】"))
        if layers:
            parts = []
            for lay in layers[:3]:
                e = lay.get("emotion", "")
                i = lay.get("intensity", 0.5)
                if e:
                    parts.append(f"{e}({int(i * 100)}%)")
            if parts:
                ctx_parts.append(emotion_header + ", ".join(parts))
        else:
            primary = emotion.get("primary_emotion", "")
            weight = emotion.get("primary_weight", 0.5)
            if primary:
                if locale == "en":
                    ctx_parts.append(f"{emotion_header} {primary} ({int(weight * 100)}%)")
                else:
                    ctx_parts.append(f"{emotion_header}{primary}（{int(weight * 100)}%）")

    # Affinity
    if affinity:
        status = affinity.get("status", "")
        ctx_parts.append(render("reflection.affinity_label", locale=locale,
                                default=("[Affinity] ($status)" if locale == "en" else "【好感度】（$status）"), status=status))

    # 用户沉默分级
    ctx_parts.append(render("reflection.user_time_label", locale=locale,
                            default=("[User Last Spoke] $silence" if locale == "en" else "【用户说话时间】$silence"),
                            silence=_format_silence_tier(silent_seconds, locale)))

    # 【你（AI）的主动发言】仅当「上次 ASE 后用户未回复」时注入
    if ase_context is not None:
        last_speak_time = ase_context.get("last_speak_time")
        user_replied_since_last_ase = (
            last_user_message_time is not None
            and last_speak_time is not None
            and last_user_message_time > last_speak_time
        )
        content = (ase_context.get("last_speak_content") or "").strip()
        if content and not user_replied_since_last_ase:
            excerpt = content[:150] + ("…" if len(content) > 150 else "")
            ctx_parts.append(render("reflection.last_spoke_label", locale=locale,
                                    default=("You last initiated with: \"$excerpt\"" if locale == "en" else "你上次主动说的是：「$excerpt」"), excerpt=excerpt))
            secs = ase_context.get("seconds_since_last_speak")
            if secs is not None:
                ctx_parts.append(render("reflection.ase_speak_ago", locale=locale,
                                        default=("[Your Proactive Speech] $minutes minutes since you last initiated" if locale == "en" else "【你的主动发言】距离你上次主动开口已经 $minutes 分钟"),
                                        minutes=int(secs / 60)))
            else:
                ctx_parts.append(get_prompt("reflection.ase_never_spoke", locale=locale,
                                            default=("[Your Proactive Speech] You have not yet initiated" if locale == "en" else "【你的主动发言】你尚未主动说过话")))
            ctx_parts.append(get_prompt("reflection.ase_user_no_reply", locale=locale,
                                        default=("Your last message was a proactive one; the user has not yet replied." if locale == "en" else "上一条消息是你主动说的，用户尚未回复。")))
        consec = ase_context.get("consecutive_speaks", 0)
        if consec and int(consec) > 0:
            ctx_parts.append(render("reflection.ase_consec_speaks", locale=locale,
                                    default=("You have initiated $consec consecutive times without a reply;" if locale == "en" else "你已连续主动发言 $consec 次，用户均未回复；"),
                                    consec=int(consec)))

    context_block = "\n\n".join(ctx_parts)

    # 最近对话
    if recent_turns:
        turns = recent_turns[-10:]
        lines = []
        for m in turns:
            sender = (m.get("sender") or "").strip()
            content = (m.get("content") or "")[:300].strip()
            is_human = sender == user_name or (not sender and m.get("role", "user") == "user")
            if is_human:
                lines.append(f"{user_label}：{content}")
            else:
                lines.append(f"{persona_name}：{content}")
        dialogue_text = "\n".join(lines)
    else:
        dialogue_text = get_prompt("reflection.no_recent_dialogue", locale=locale,
                                   default=("No recent conversation." if locale == "en" else "暂无最近对话。"))

    prompt_base = _build_system_prompt(persona_name, persona_brief,
                                       context_notes or [], ase_decision_context, locale)
    dialogue_header = get_prompt("reflection.recent_dialogue_header", locale=locale,
                                 default=("\n\n[Recent Conversation]\n" if locale == "en" else "\n\n【最近对话】\n"))
    context_header  = get_prompt("reflection.context_header", locale=locale,
                                 default=("\n\n[Current Context]\n" if locale == "en" else "\n\n【当前上下文】\n"))
    system_content = prompt_base + dialogue_header + dialogue_text + context_header + context_block

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": _reflection_user_instruction(locale)},
    ]


# 合法 reflection JSON 的键（仅此六种，否则视为错误格式）
_REFLECTION_KEYS = frozenset({"thought", "style_hint", "urgency", "topic_hint", "next_reflection_in", "speak_reason"})
_VALID_SPEAK_REASONS = frozenset({"memory_recall", "trend_share", "emotional_overflow", "silence_concern", "none"})


async def _parse_reflection_response(text: str) -> Dict[str, Any]:
    """Parse JSON from LLM response; return safe defaults on error or wrong schema."""
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return {"thought": "", "style_hint": "", "urgency": 0.0, "topic_hint": ""}
        # 若模型输出了错误 schema（如 情感/认知/意图/担忧/期待），拒绝使用并回退默认
        extra = set(data.keys()) - _REFLECTION_KEYS
        if extra and not data.get("thought"):
            logger.warning(
                "[reflection] 忽略错误 JSON 格式（含非预期键 %s），请模型只输出 thought/style_hint/urgency/topic_hint",
                list(extra)[:5],
            )
            return {"thought": "", "style_hint": "", "urgency": 0.0, "topic_hint": ""}
        raw_nri = data.get("next_reflection_in")
        next_reflection_in = None
        if isinstance(raw_nri, (int, float)) and raw_nri > 0:
            next_reflection_in = int(raw_nri)
        raw_sr = str(data.get("speak_reason", "none")).strip().lower()
        speak_reason = raw_sr if raw_sr in _VALID_SPEAK_REASONS else "none"
        return {
            "thought": str(data.get("thought", "")).strip(),
            "style_hint": str(data.get("style_hint", "")).strip(),
            "urgency": float(max(0.0, min(1.0, data.get("urgency", 0.0)))),
            "topic_hint": str(data.get("topic_hint", "")).strip(),
            "next_reflection_in": next_reflection_in,
            "speak_reason": speak_reason,
        }
    except Exception:
        return {"thought": "", "style_hint": "", "urgency": 0.0, "topic_hint": "", "next_reflection_in": None}


# ── ReflectionEngine ─────────────────────────────────────────────────────────

class ReflectionEngine:
    """Background asyncio task that periodically reflects on each active session."""

    def __init__(self, app):
        self._app = app
        self._task: Optional[asyncio.Task] = None
        self._next_run_at: float = 0.0       # wall-clock timestamp of next scheduled wake-up
        self._last_skip_reason: str = ""     # most recent skip reason from _reflect()
        self._suppress_idle_until: float = 0.0  # suppress idle throttle until this timestamp

    async def start(self):
        """Start the background reflection loop."""
        self._task = asyncio.create_task(self._loop(), name="reflection_engine")
        cfg = self._app.state.config.get_reflection_config()
        logger.info("[reflection] engine started (interval=%ds)", cfg.get("interval_seconds", 180))

    async def stop(self):
        """Cancel the background loop gracefully."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[reflection] engine stopped")

    async def _loop(self):
        """Main reflection loop — runs until cancelled."""
        app = self._app
        cfg = app.state.config.get_reflection_config()
        interval = int(cfg.get("interval_seconds", 180))

        logger.info("[reflection] loop starting, interval=%ds", interval)

        # 首轮延迟：若存在上次自省时间则计算剩余等待时间，避免重启后仍等满 N 秒
        # 优先使用模型上次输出的 next_reflection_in；缺失时回退到 interval_seconds
        sm = app.state.session_manager
        current = sm.get_current() if sm else None
        if current and getattr(current, "reflection_state", None):
            updated_at = (current.reflection_state or {}).get("updated_at") or 0
            next_in = (current.reflection_state or {}).get("next_reflection_in")
            if updated_at > 0:
                if next_in is not None:
                    remaining = (updated_at + next_in) - time.time()
                else:
                    remaining = interval - (time.time() - updated_at)
                if remaining > 0:
                    logger.debug("[reflection] resuming after restart, sleeping %.0fs", remaining)
                    await asyncio.sleep(remaining)
                # else: overdue → reflect immediately
            else:
                await asyncio.sleep(interval)
        else:
            await asyncio.sleep(interval)

        prev_session_id: Optional[str] = None

        while True:
            current = None
            session_switched = False
            try:
                cfg = app.state.config.get_reflection_config()
                if not cfg.get("enabled"):
                    logger.debug("[reflection] disabled, sleeping...")
                    await asyncio.sleep(interval)
                    continue

                interval = int(cfg.get("interval_seconds", 180))
                sm = app.state.session_manager
                current = sm.get_current()

                # Detect session switch so idle throttle can be suppressed
                current_id = current.id if current else None
                session_switched = current_id != prev_session_id
                if session_switched:
                    logger.debug(
                        "[reflection] session switched: %s → %s",
                        prev_session_id, current_id,
                    )
                    # Suppress idle throttle for 2 full intervals so the new session
                    # gets at least 2 normal-speed reflections before going idle.
                    self._suppress_idle_until = time.time() + interval * 2
                prev_session_id = current_id

                reflect_failed = False
                reflected_session = None
                for session in sm.list_sessions():
                    # 只对当前活跃 session 执行自省，切换走后立即静止
                    if not current or session.id != current.id:
                        continue
                    # 人格级别覆盖：该人格单独关闭了自省则跳过
                    try:
                        from src.config.effective_config import get_effective_engine_config
                        eff = get_effective_engine_config(app, session.profile_id, "reflection")
                        if not eff.get("enabled", True):
                            continue
                    except Exception:
                        pass
                    reflected_session = session
                    try:
                        await self._reflect(session, cfg)
                    except Exception as e:
                        reflect_failed = True
                        logger.warning("[reflection] session=%s error: %s", session.id, e)
                        log_error("reflection", str(e), {"session_id": session.id}, traceback_str=traceback.format_exc())

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[reflection] loop error: %s", e)
                log_error("reflection", str(e), {"session_id": current.id if current else None}, traceback_str=traceback.format_exc())
                reflect_failed = True

            # 计算下次自省间隔：
            # 1. 失败时用 retry_seconds 尽快重试
            # 2. 模型输出了 next_reflection_in 时用其值（clamp 到 [min, max]）
            # 3. 否则回退旧的 idle_throttle 逻辑
            min_s = int(cfg.get("interval_min_seconds", 20))
            max_s = int(cfg.get("interval_max_seconds", 1800))
            retry_s = int(cfg.get("retry_seconds", 60))

            if reflect_failed:
                sleep_duration = retry_s
                logger.debug("[reflection] reflect failed, retrying in %ds", sleep_duration)
            else:
                next_in = (reflected_session.reflection_state or {}).get("next_reflection_in") if reflected_session else None
                if next_in is not None:
                    sleep_duration = max(min_s, min(max_s, int(next_in)))
                    logger.debug("[reflection] model scheduled next in %ds (clamped to %ds)", next_in, sleep_duration)
                else:
                    # Fallback: 旧的 idle_throttle 逻辑
                    idle_throttle_after = float(cfg.get("idle_throttle_after_seconds", 0))
                    idle_interval = int(cfg.get("idle_interval_seconds", interval * 5))
                    sleep_duration = interval
                    if idle_throttle_after > 0 and current and time.time() >= self._suppress_idle_until:
                        silent_secs = time.time() - current.last_user_message_time
                        if silent_secs >= idle_throttle_after:
                            sleep_duration = max(interval, idle_interval)
                            logger.debug(
                                "[reflection] idle throttle: silent=%.0fs >= %.0fs, next in %ds",
                                silent_secs, idle_throttle_after, sleep_duration,
                            )

            self._next_run_at = time.time() + sleep_duration
            await asyncio.sleep(sleep_duration)

    async def _reflect(self, session, cfg: Dict[str, Any]):
        """Run one reflection cycle for a single session."""
        from src.utils.debug_logger import log_reflection_result, log_reflection_skip

        recent_turns_n = int(cfg.get("recent_turns", 10))

        # Load conversation history (merged single+group when merge enabled)
        store = session.conversation_store
        profile_data: Dict[str, Any] = {}
        try:
            profile_path = os.path.join(get_project_root(), "profiles", f"{session.profile_id}.json")
            if os.path.exists(profile_path):
                with open(profile_path, encoding="utf-8") as f:
                    profile_data = json.load(f)
        except Exception:
            pass
        mem = (profile_data.get("memory_config") or {})
        if mem.get("group_chat_merge_into_history") is not False and getattr(self._app, "state", None) and getattr(self._app.state, "group_manager", None):
            from src.memory.merged_history import get_merged_recent_messages
            merged = get_merged_recent_messages(
                session, self._app, profile_data, recent_turns_n, cap=None
            )
            # Convert to {role, content, sender} for _build_reflection_prompt; group messages get prefix in content
            recent = []
            for m in merged:
                role = m.get("role") or "user"
                content = (m.get("content") or "").strip()
                sender = (m.get("sender") or "").strip()
                if m.get("source") == "group":
                    content = f"【群「{m.get('gname', '')}」】{sender}: {content}"
                recent.append({"role": role, "content": content, "sender": sender})
        else:
            recent = store.get_recent(n_turns=recent_turns_n)

        if not recent:
            log_reflection_skip(session.id, "no_recent_turns")
            logger.debug("[reflection] session=%s: no recent turns, skipping", session.id)
            self._last_skip_reason = "no_recent_turns"
            return

        # Read emotion + affinity state (best-effort)
        emotion = self._load_emotion(session.profile_id)
        affinity = self._load_affinity(session.profile_id)
        silent_seconds = time.time() - session.last_user_message_time

        # Get persona name and brief for character-aware reflection
        persona_name = session.display_name or "AI"
        persona_brief = ""
        pdata: Dict[str, Any] = {}
        try:
            profile_path = os.path.join(get_project_root(), "profiles", f"{session.profile_id}.json")
            if os.path.exists(profile_path):
                with open(profile_path, encoding="utf-8") as f:
                    pdata = json.load(f)
                # 与情感分类、好感度调整同源：reflection_config.custom_prompt（截断见 persona_context）
                persona_brief = get_persona_context_for_secondary_llm(pdata)
        except Exception:
            pass

        # Build time + cadence context notes for urgency calibration
        context_notes = []
        try:
            from datetime import datetime
            from src.prompt.segments.time_context import get_time_context_notes
            tracker = getattr(getattr(self._app, "state", None), "cadence_tracker", None)
            cadence = tracker.get_signals(session.profile_id) if tracker else None
            profile_data = {}
            if os.path.exists(profile_path):
                with open(profile_path, encoding="utf-8") as f:
                    profile_data = json.load(f)
            context_notes = get_time_context_notes(
                now=datetime.now(),
                profile=profile_data,
                cadence_signals=cadence,
                session=session,
            )
        except Exception as _ctx_err:
            logger.debug("[reflection] context_notes 构建失败: %s", _ctx_err)

        # ASE 决策线：与 ASE 门控一致的 mode 配置，供自省校准 urgency
        ase_decision_context = None
        try:
            ase_cfg = self._app.state.config.get_ase_config()
            if ase_cfg.get("enabled"):
                mode_name = getattr(session, "ase_mode", "medium")
                if mode_name == "medium":
                    mode_name = ase_cfg.get("mode", "medium")
                modes = ase_cfg.get("modes", {})
                mode_cfg = modes.get(mode_name, modes.get("medium", {}))
                ase_decision_context = {
                    "mode_name": mode_name,
                    "urgency_threshold": float(mode_cfg.get("urgency_threshold", 0.35)),
                    "min_silent_seconds": float(mode_cfg.get("min_silent_seconds", 300)),
                    "min_interval": float(mode_cfg.get("min_interval", 600)),
                    "max_per_24h": mode_cfg.get("max_per_24h"),
                    "max_consecutive_without_response": mode_cfg.get("max_consecutive_without_response"),
                }
        except Exception as _ase_err:
            logger.debug("[reflection] ase_decision_context 构建失败: %s", _ase_err)

        # 上次主动说话的时间与内容（供自省校准 urgency 与避免重复）
        ase_context = None
        try:
            from src.core.ase import get_ase_context_for_reflection
            ase_context = get_ase_context_for_reflection(session.storage_root)
        except Exception as _ase_ctx_err:
            logger.debug("[reflection] ase_context 读取失败: %s", _ase_ctx_err)

        # Build prompt via unified segment system
        prev_reflection = getattr(session, "reflection_state", None)
        _app_cfg = getattr(getattr(self._app, "state", None), "config", None)
        user_name = getattr(_app_cfg, "user_name", "用户") or "用户"
        user_persona_global = getattr(_app_cfg, "user_persona", {}) or {}
        user_persona = {**user_persona_global, **(pdata.get("user_persona") or {})}
        # Gather memory_manager for memory_facts segment
        _mm = None
        try:
            managers = getattr(getattr(self._app, "state", None), "memory_managers", {})
            _mm = managers.get(session.profile_id)
        except Exception:
            pass

        # Global reflection config for recent_dialogue max_turns etc.
        _ref_cfg = {}
        try:
            _ref_cfg = self._app.state.config.get_reflection_config() or {}
        except Exception:
            pass

        ctx = ReflectionBuildContext(
            profile_id=session.id,
            profile=pdata,
            recent_turns=recent,
            emotion=emotion,
            affinity=affinity,
            silent_seconds=silent_seconds,
            persona_name=persona_name,
            persona_brief=persona_brief,
            ase_context=ase_context,
            prev_reflection=prev_reflection,
            context_notes=context_notes or [],
            ase_decision_context=ase_decision_context,
            last_user_message_time=getattr(session, "last_user_message_time", None),
            user_name=user_name,
            user_persona=user_persona,
            locale=get_locale(),
            store=store,
            memory_manager=_mm,
            reflection_cfg=_ref_cfg,
        )
        messages = build_reflection_messages(session.id, ctx)

        preset, used_fallback, model_name = self._get_preset()
        if preset is None:
            log_reflection_skip(session.id, "model_disabled")
            logger.debug("[reflection] no reflection preset available, skipping")
            return

        # Gen params: only pass keys explicitly set by user — unset keys fall through to preset's _gen_kwargs.
        # max_tokens is special: always cap at REFLECTION_MAX_TOKENS to avoid runaway generation.
        _sec_ref = getattr(self._app.state, "config", None)
        _sec_ref = getattr(_sec_ref, "secondary_models", {}).get("reflection", {}) if _sec_ref else {}
        _gp = _sec_ref.get("gen_params") or {}
        _call_kwargs = {k: _gp[k] for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty") if k in _gp}
        _call_kwargs["max_tokens"] = _gp.get("max_tokens", REFLECTION_MAX_TOKENS)

        from src.utils.debug_logger import log_secondary_llm_call, log_secondary_llm_response

        log_secondary_llm_call(
            role="reflection",
            messages=messages,
            model=model_name,
            gen_kwargs=_call_kwargs,
            session_id=session.id,
        )

        t0 = time.time()
        try:
            from src.llm.registry import get_provider
            provider = get_provider(preset)
            raw = await provider.chat(messages, **_call_kwargs)
            # chat() swallows exceptions — detect error string and re-raise so fallback kicks in
            if raw.startswith("[LLM Error:"):
                raise RuntimeError(raw)
        except Exception as e:
            log_reflection_skip(session.id, "exception", error=str(e))
            logger.warning("[reflection] session=%s LLM error: %s", session.id, e)
            # Try fallback if primary failed
            if not used_fallback:
                fallback_preset = self._get_fallback_preset()
                if fallback_preset:
                    try:
                        used_fallback = True
                        model_name = fallback_preset.get("model", "")
                        log_secondary_llm_call(
                            role="reflection",
                            messages=messages,
                            model=model_name,
                            gen_kwargs=_call_kwargs,
                            session_id=session.id,
                        )
                        provider = get_provider(fallback_preset)
                        raw = await provider.chat(messages, **_call_kwargs)
                        if raw.startswith("[LLM Error:"):
                            raise RuntimeError(raw)
                    except Exception as e2:
                        log_reflection_skip(session.id, "exception", error=str(e2))
                        from src.utils.engine_warnings import set_warning
                        set_warning(self._app, "reflection",
                                    f"反思引擎主/备模型均失败({type(e2).__name__}) — 自省暂停")
                        return
                else:
                    from src.utils.engine_warnings import set_warning
                    set_warning(self._app, "reflection",
                                f"反思引擎调用失败({type(e).__name__}) — 自省暂停")
                    return
            else:
                from src.utils.engine_warnings import set_warning
                set_warning(self._app, "reflection",
                            f"反思引擎调用失败({type(e).__name__}) — 自省暂停")
                return

        duration_ms = int((time.time() - t0) * 1000)
        log_secondary_llm_response(
            role="reflection",
            response=raw,
            model=model_name,
            session_id=session.id,
            duration_ms=duration_ms,
        )
        result = await _parse_reflection_response(raw)

        # Retry once on truncated response (very short raw + empty thought) — 仍用主模型
        if not result["thought"] and len(raw) < 80 and not used_fallback:
            logger.debug("[reflection] session=%s truncated (%d chars), retrying primary", session.id, len(raw))
            try:
                raw_retry = await provider.chat(messages, **_call_kwargs)
                if not raw_retry.startswith("[LLM Error:"):
                    result_retry = await _parse_reflection_response(raw_retry)
                    if result_retry["thought"]:
                        raw, result = raw_retry, result_retry
            except Exception:
                pass

        # 主模型返回空/无效时，尝试备用模型（与 exception 时一致）
        if not result["thought"] and not used_fallback:
            fallback_preset = self._get_fallback_preset()
            if fallback_preset:
                logger.debug("[reflection] session=%s empty_response, trying fallback model", session.id)
                try:
                    used_fallback = True
                    model_name = fallback_preset.get("model", "")
                    log_secondary_llm_call(
                        role="reflection",
                        messages=messages,
                        model=model_name,
                        gen_kwargs=_call_kwargs,
                        session_id=session.id,
                    )
                    t_fb = time.time()
                    provider_fb = get_provider(fallback_preset)
                    raw = await provider_fb.chat(messages, **_call_kwargs)
                    duration_ms = int((time.time() - t_fb) * 1000)
                    if raw.startswith("[LLM Error:"):
                        raise RuntimeError(raw)
                    log_secondary_llm_response(
                        role="reflection",
                        response=raw,
                        model=model_name,
                        session_id=session.id,
                        duration_ms=duration_ms,
                    )
                    result = await _parse_reflection_response(raw)
                except Exception as e_fb:
                    logger.warning("[reflection] session=%s fallback also failed: %s", session.id, e_fb)
                    log_reflection_skip(session.id, "empty_response", error=str(e_fb))
                    return

        if not result["thought"]:
            logger.debug("[reflection] session=%s empty_response raw=%r", session.id, raw[:120])
            log_reflection_skip(session.id, "empty_response")
            return

        # 成功：清除反思警告
        from src.utils.engine_warnings import clear_warning
        clear_warning(self._app, "reflection")

        # Store in session and persist to disk
        session.reflection_state = {
            "thought": result["thought"],
            "style_hint": result["style_hint"],
            "urgency": result["urgency"],
            "topic_hint": result["topic_hint"],
            "next_reflection_in": result.get("next_reflection_in"),
            "speak_reason": result.get("speak_reason", "none"),
            "updated_at": time.time(),
        }
        session.save_runtime_state()

        log_reflection_result(
            session_id=session.id,
            profile_id=session.profile_id,
            thought=result["thought"],
            style_hint=result["style_hint"],
            urgency=result["urgency"],
            topic_hint=result["topic_hint"],
            model=model_name,
            used_fallback=used_fallback,
            recent_turns_used=len(recent),
            duration_ms=duration_ms,
        )
        logger.debug(
            "[reflection] session=%s urgency=%.2f thought=%s",
            session.id, result["urgency"], result["thought"][:50]
        )

    def _get_preset(self):
        """Return (preset_dict, used_fallback, model_name). Returns (None, False, '') if unavailable."""
        cfg = self._app.state.config
        sec = cfg.secondary_models.get("reflection", {})
        if not sec.get("enabled", True):
            return None, False, ""

        primary_name, fallback_name = cfg.get_reflection_model_names()

        if primary_name and primary_name in cfg.llm_presets:
            preset = cfg.llm_presets[primary_name]
            return preset, False, preset.get("model", primary_name)

        if fallback_name and fallback_name in cfg.llm_presets:
            preset = cfg.llm_presets[fallback_name]
            return preset, True, preset.get("model", fallback_name)

        return None, False, ""

    def _get_fallback_preset(self):
        """Return the fallback preset dict, or None."""
        cfg = self._app.state.config
        _, fallback_name = cfg.get_reflection_model_names()
        if fallback_name and fallback_name in cfg.llm_presets:
            return cfg.llm_presets[fallback_name]
        return None

    def _load_emotion(self, profile_id: str) -> Optional[Dict]:
        """Load emotion state from disk (best-effort)."""
        import json, os
        path = os.path.join(get_project_root(), "profiles", profile_id, "emotion_state.json")
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    def _load_affinity(self, profile_id: str) -> Optional[Dict]:
        """Load affinity state from disk (best-effort)."""
        import json, os
        path = os.path.join(get_project_root(), "profiles", profile_id, "affinity_state.json")
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return None
