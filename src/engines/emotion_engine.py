"""EmotionEngine — 管理 profiles/<id>/emotion_state.json（情绪 + 能量）。

三条触发路径：
  - 后台 EnergyRefreshTask（每 5 分钟）：apply_time_recovery() + apply_emotion_decay()
  - Chat 流完成后 fire-and-forget：maybe_classify()
"""
import json
import logging
import os
import time
import traceback
from string import Template
from typing import Any, Dict, List, Optional

from src.config.prompt_loader import get_dict, get_locale, get_prompt, render
from src.utils.persona_context import get_persona_context_for_secondary_llm
from src.utils.debug_logger import log_error

logger = logging.getLogger(__name__)

# 默认恢复/满回时间（可被 config 的 energy.recovery_gain_per_300s、full_recovery_secs 覆盖）
_ENERGY_RECOVERY_PER_300S = 2.0
_ENERGY_FULL_RECOVERY_SECS = 14400  # 4h

# 情绪自动衰减默认值（可被 config 的 emotion.decay_* 覆盖）
_EMOTION_DECAY_FULL_SECS = 14400          # 静默 4h 后情绪回归中性
_EMOTION_DECAY_SKIP_IF_ACTIVE_SECS = 300  # 对话活跃期不衰减
_EMOTION_DECAY_NEUTRAL = "calm"

# 情绪→能量即时影响
# 注意：正向情绪的影响值不应过大，否则会形成反馈死循环：
#   活泼角色语气 → 分类器给正向情绪 → 能量暴涨 → 更亢奋的prompt → 更活泼的语气 → 循环
_ENERGY_IMPACT: Dict[str, float] = {
    "excited":        8.0,
    "inspired":       8.0,
    "joyful":         8.0,
    "playful":        5.0,
    "playful_teasing": 5.0,
    "confident":      5.0,
    "tired":         -25.0,
    "sad":           -25.0,
    "anxious":       -25.0,
    "frustrated":    -25.0,
}

_DEFAULT_STATE: Dict[str, Any] = {
    # Canonical multi-layer format (up to 3 layers)
    "emotion_layers":     [{"emotion": "calm", "intensity": 1.0}],
    # Legacy scalar fields kept for backward-compat (always synced from emotion_layers)
    "primary_emotion":    "calm",
    "primary_weight":     1.0,
    "secondary_emotion":  None,
    "secondary_weight":   0.0,
    "tertiary_emotion":   None,
    "tertiary_weight":    0.0,
    "energy_level":       80.0,
    "last_updated":       0.0,
    "last_energy_recalc": 0.0,
    "classifier_call_count": 0,
    "_prev_primary":      None,
}



class EmotionEngine:
    """情绪与能量状态引擎。"""

    def _state_path(self, session) -> str:
        return os.path.join(session.storage_root, "emotion_state.json")

    def load_state(self, session) -> Dict[str, Any]:
        path = self._state_path(session)
        if not os.path.exists(path):
            state = dict(_DEFAULT_STATE)
            now = time.time()
            state["last_updated"] = now
            state["last_energy_recalc"] = now
            return state
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 补全缺失字段（老数据兼容）
            for k, v in _DEFAULT_STATE.items():
                data.setdefault(k, v)
            # 向前兼容：若 emotion_layers 缺失，从 legacy 字段重建
            if not data.get("emotion_layers"):
                layers = [{"emotion": data.get("primary_emotion", "calm"),
                           "intensity": data.get("primary_weight", 1.0)}]
                if data.get("secondary_emotion"):
                    layers.append({"emotion": data["secondary_emotion"],
                                   "intensity": data.get("secondary_weight", 0.3)})
                if data.get("tertiary_emotion"):
                    layers.append({"emotion": data["tertiary_emotion"],
                                   "intensity": data.get("tertiary_weight", 0.2)})
                data["emotion_layers"] = layers
            return data
        except Exception as e:
            logger.warning("[EmotionEngine] load_state failed for %s: %s", session.id, e)
            state = dict(_DEFAULT_STATE)
            now = time.time()
            state["last_updated"] = now
            state["last_energy_recalc"] = now
            return state

    def save_state(self, session, state: Dict[str, Any]) -> None:
        path = self._state_path(session)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning("[EmotionEngine] save_state failed for %s: %s", session.id, e)

    def get_emotion_prompts(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        """返回 profile 的 emotion_prompts；不存在则 fallback 到 default_emotions.json。"""
        prompts = (
            profile.get("emotion_config", {}).get("emotion_prompts") or {}
        )
        if prompts:
            return prompts
        return get_dict("emotion_prompts", locale=get_locale())

    def get_emotion_zh_descriptions(self, profile: Dict[str, Any]) -> Dict[str, str]:
        """返回情绪的中文简述 dict。"""
        descs = (
            profile.get("emotion_config", {}).get("emotion_zh_descriptions") or {}
        )
        if descs:
            return descs
        return get_dict("zh_descriptions", locale=get_locale())

    def apply_time_recovery(
        self,
        session,
        skip_if_active_secs: float = 300.0,
        refresh_interval: int = 300,
        recovery_gain_per_300s: Optional[float] = None,
        full_recovery_secs: Optional[float] = None,
    ) -> None:
        """按离线时长线性恢复能量。由 EnergyRefreshTask 定期调用。

        线性速率：recovery_gain_per_300s 点 / 300s（未达 full_recovery_secs 时）。
        离线超过 full_recovery_secs 直接恢复至 100。

        skip_if_active_secs > 0：若距上次用户消息不足该秒数，视为对话活跃期，
        跳过被动恢复。
        refresh_interval：仅当 elapsed >= refresh_interval 时才执行恢复（人格级可配置）。
        """
        now = time.time()
        if skip_if_active_secs > 0:
            last_msg = getattr(session, "last_user_message_time", 0.0)
            silent_secs = now - last_msg
            if silent_secs < skip_if_active_secs:
                logger.debug(
                    "[EmotionEngine] skip time_recovery (active %.0fs ago) session=%s",
                    silent_secs, session.id,
                )
                return

        state = self.load_state(session)
        elapsed = now - state.get("last_energy_recalc", now)
        if elapsed < 60:
            return   # 不足 1 分钟，跳过
        if elapsed < refresh_interval:
            return   # 未达该人格配置的恢复间隔，跳过

        gain_per_300s = recovery_gain_per_300s if recovery_gain_per_300s is not None else _ENERGY_RECOVERY_PER_300S
        full_secs = full_recovery_secs if full_recovery_secs is not None else _ENERGY_FULL_RECOVERY_SECS
        energy = state["energy_level"]
        if elapsed >= full_secs:
            energy = 100.0
        else:
            gain = (elapsed / 300.0) * gain_per_300s
            energy = min(100.0, energy + gain)

        state["energy_level"] = energy
        state["last_energy_recalc"] = now
        state["last_updated"] = now
        self.save_state(session, state)
        logger.info(
            "[EmotionEngine] time_recovery session=%s energy=%.1f elapsed=%.0fs",
            session.id, energy, elapsed,
        )

    def apply_emotion_decay(
        self,
        session,
        decay_full_secs: Optional[float] = None,
        skip_if_active_secs: float = 300.0,
        neutral_emotion: str = _EMOTION_DECAY_NEUTRAL,
    ) -> bool:
        """情绪硬截断衰减：用户静默超过 decay_full_secs 后，把多层情绪整体重置为中性。

        基准时间取 session.last_user_message_time（用户沉默时长）。
        注意：不能用 state["last_updated"] —— 它会被 apply_time_recovery /
        apply_message_cost / apply_emotion_to_energy 频繁刷新成 now，
        导致 elapsed 永远归零、衰减永不触发。

        返回 True 表示发生了重置（用于日志/统计）。
        """
        now = time.time()
        full_secs = decay_full_secs if decay_full_secs is not None else _EMOTION_DECAY_FULL_SECS
        if full_secs <= 0:
            return False

        state = self.load_state(session)
        if state.get("primary_emotion") == neutral_emotion:
            return False

        last_msg = getattr(session, "last_user_message_time", 0.0) or 0.0
        if last_msg <= 0:
            return False  # 无法判定沉默时长，跳过
        silent_secs = now - last_msg

        if skip_if_active_secs > 0 and silent_secs < skip_if_active_secs:
            return False

        elapsed = silent_secs
        if elapsed < full_secs:
            return False

        prev_primary = state.get("primary_emotion")
        state["_prev_primary"] = prev_primary
        state["emotion_layers"] = [{"emotion": neutral_emotion, "intensity": 1.0}]
        state["primary_emotion"] = neutral_emotion
        state["primary_weight"] = 1.0
        state["secondary_emotion"] = None
        state["secondary_weight"] = 0.0
        state["tertiary_emotion"] = None
        state["tertiary_weight"] = 0.0
        state["last_updated"] = now
        self.save_state(session, state)
        logger.info(
            "[EmotionEngine] emotion_decay session=%s %s -> %s (silent %.0fs)",
            session.id, prev_primary, neutral_emotion, elapsed,
        )
        return True

    def apply_emotion_to_energy(self, session, emotion: str) -> None:
        """情绪分类更新后立即调整能量。"""
        impact = _ENERGY_IMPACT.get(emotion, 0.0)
        if impact == 0.0:
            return
        state = self.load_state(session)
        state["energy_level"] = max(0.0, min(100.0, state["energy_level"] + impact))
        state["last_updated"] = time.time()
        self.save_state(session, state)
        logger.info(
            "[EmotionEngine] emotion_energy_impact session=%s emotion=%s impact=%.1f energy=%.1f",
            session.id, emotion, impact, state["energy_level"],
        )

    def apply_message_cost(self, session, cost: float = 2.0) -> None:
        """每条 AI 回复完成后扣减能量（对话消耗）。"""
        state = self.load_state(session)
        state["energy_level"] = max(0.0, state["energy_level"] - cost)
        state["last_updated"] = time.time()
        self.save_state(session, state)
        logger.debug(
            "[EmotionEngine] message_cost session=%s cost=%.1f energy=%.1f",
            session.id, cost, state["energy_level"],
        )

    async def maybe_classify(
        self,
        session,
        recent_ai_msgs: List[str],
        app,
    ) -> bool:
        """每 N 条 AI 回复触发一次情绪 LLM 分类。返回 True 若情绪发生变化。"""
        from src.config.effective_config import get_effective_engine_config
        engine_cfg = get_effective_engine_config(app, session.profile_id, "emotion")
        if engine_cfg.get("enabled", True) is False:
            return False
        freq = engine_cfg.get("classification_frequency", 5)

        state = self.load_state(session)
        count = state.get("classifier_call_count", 0)

        state["classifier_call_count"] = count + 1
        self.save_state(session, state)

        if count % freq != 0:
            return False

        if not recent_ai_msgs:
            return False

        return await self._do_classify(session, recent_ai_msgs, app)

    async def _do_classify(
        self,
        session,
        recent_ai_msgs: List[str],
        app,
    ) -> bool:
        """调用 analysis LLM 分类情绪，更新 emotion_state.json。"""
        import time as _time
        from src.llm.registry import get_provider_for_role
        from src.utils.debug_logger import (
            log_classify_call, log_emotion_entry,
            log_secondary_llm_call, log_secondary_llm_response,
        )

        provider = get_provider_for_role("analysis")
        if provider is None:
            logger.debug("[EmotionEngine] no analysis provider, skipping classify")
            from src.utils.engine_warnings import set_warning
            set_warning(app, "analysis", "辅助模型未配置/未启用 — 情绪分类与好感调整暂停")
            return False

        state = self.load_state(session)
        profile = getattr(session, "profile", {})
        if not isinstance(profile, dict):
            try:
                profile = vars(profile)
            except Exception:
                profile = {}
        if not profile and getattr(session, "profile_id", None):
            try:
                import json as _json
                import os as _os
                from src.utils.paths import get_project_root as _get_root
                _path = _os.path.join(_get_root(), "profiles", f"{session.profile_id}.json")
                if _os.path.exists(_path):
                    with open(_path, encoding="utf-8") as _f:
                        profile = _json.load(_f)
            except Exception:
                pass
        # 单聊且合并开启时，用合并近期；从合并列表取 recent_ai_msgs
        mem = (profile.get("memory_config") or {})
        merged_for_emotion = None
        if mem.get("group_chat_merge_into_history") is not False and app and getattr(getattr(app, "state", None), "group_manager", None):
            try:
                from src.memory.merged_history import get_merged_recent_messages
                merged_for_emotion = get_merged_recent_messages(session, app, profile, 6, cap=None)
                recent_ai_msgs = [m.get("content", "") for m in merged_for_emotion if m.get("role") == "assistant"][-3:]
            except Exception:
                pass

        emotion_keys = list(self.get_emotion_prompts(profile).keys())
        # [public] extended key filter omitted
        if not emotion_keys:
            logger.debug("[EmotionEngine] no emotion_prompts, skipping classify")
            return False

        zh_descs = self.get_emotion_zh_descriptions(profile)
        emotion_list_with_zh = "\n".join(
            f"- {k}: {zh_descs.get(k, k)}" for k in emotion_keys
        )

        msgs_text = "\n".join(f"- {m}" for m in recent_ai_msgs[-3:])

        # 用于 user_sentiment：单聊且合并开启时用上面得到的 merged，否则用 store
        user_msgs_text = ""
        try:
            store = session.conversation_store
            user_name = getattr(getattr(app, "state", None) and getattr(app.state, "config", None), "user_name", "用户") or "用户"
            if merged_for_emotion is not None:
                recent_turns = [{"role": m.get("role"), "content": m.get("content"), "sender": m.get("sender")} for m in merged_for_emotion]
            else:
                recent_turns = store.get_recent(n_turns=6)
            user_lines = [
                m["content"] for m in recent_turns
                if (m.get("sender") or "").strip() == user_name or (not (m.get("sender") or "").strip() and m.get("role") == "user")
            ][-3:]
            user_msgs_text = "\n".join(f"- {m}" for m in user_lines)
        except Exception:
            pass

        # Build JSON example and user_sentiment instruction together so the LLM
        # sees user_sentiment INSIDE the example object (not appended outside it).
        if user_msgs_text:
            json_example = (
                f'{{"layers": [{{"emotion": "主情绪", "intensity": 0.85}}, '
                f'{{"emotion": "次情绪", "intensity": 0.4}}], '
                f'"reason": "一句话说明分类理由", "user_sentiment": "positive"}}'
            )
            user_sentiment_block = Template(get_prompt("user_sentiment")).safe_substitute(
                user_msgs_text=user_msgs_text
            )
        else:
            json_example = (
                f'{{"layers": [{{"emotion": "主情绪", "intensity": 0.85}}, '
                f'{{"emotion": "次情绪", "intensity": 0.4}}, '
                f'{{"emotion": "三情绪", "intensity": 0.2}}], "reason": "一句话说明分类理由"}}'
            )
            user_sentiment_block = ""

        core = Template(get_prompt("classification")).safe_substitute(
            emotion_list_with_zh=emotion_list_with_zh,
            json_example=json_example,
            msgs_text=msgs_text,
        ) + user_sentiment_block
        persona_ctx = get_persona_context_for_secondary_llm(profile)
        if persona_ctx:
            preamble = render(
                "analysis_shared_context",
                locale=get_locale(),
                persona_context=persona_ctx,
            ).strip()
            prompt = f"{preamble}\n\n{core}" if preamble else core
        else:
            prompt = core
        messages = [{"role": "user", "content": prompt}]
        model_name = getattr(provider, "model", "unknown")

        log_secondary_llm_call(
            role="emotion",
            messages=messages,
            model=model_name,
            gen_kwargs={},
            session_id=session.id,
        )

        try:
            _t0 = _time.time()
            response_text = ""
            _llm_exc = None
            for _attempt in range(2):  # 最多重试 1 次
                try:
                    response_text = ""
                    async for token in provider.stream_chat(messages):
                        response_text += token
                    _llm_exc = None
                    break
                except Exception as _e:
                    _llm_exc = _e
                    if _attempt == 0:
                        logger.warning("[EmotionEngine] stream_chat 失败，重试 session=%s: %s",
                                       session.id, _e)
                        import asyncio as _asyncio
                        await _asyncio.sleep(3.0)
            if _llm_exc:
                raise _llm_exc

            log_secondary_llm_response(
                role="emotion",
                response=response_text,
                model=model_name,
                session_id=session.id,
                duration_ms=int((_time.time() - _t0) * 1000),
            )

            # 提取 JSON（跳过 markdown 代码块）
            raw = response_text.strip()
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                # Fallback: some models append fields outside the closing brace, e.g.:
                #   {"layers":[...],"reason":"..."}, "user_sentiment": "excited"}
                # raw_decode() parses the first valid JSON object and returns the rest.
                import re as _re
                try:
                    result, _rest = json.JSONDecoder().raw_decode(raw)
                except json.JSONDecodeError:
                    logger.warning(
                        "[EmotionEngine] unparseable emotion response for session=%s raw=%r, skipping",
                        session.id, raw[:120],
                    )
                    return False
                # Try to recover any top-level key:value pairs from the trailing garbage
                for _m in _re.finditer(r'"(\w+)"\s*:\s*"([^"]*)"', _rest):
                    result.setdefault(_m.group(1), _m.group(2))
                logger.debug(
                    "[EmotionEngine] JSON fallback parse used for session=%s rest=%r",
                    session.id, _rest[:80],
                )

            if result is None:
                logger.warning("[EmotionEngine] emotion response is null, skipping")
                return False
            # 部分模型会返回「多条消息各一个对象」的数组 [{...}, {...}]，取最后一条（最近一条消息）作为结果
            if isinstance(result, list):
                if result and isinstance(result[-1], dict):
                    result = result[-1]
                    logger.debug("[EmotionEngine] emotion response was array, using last element")
                else:
                    logger.warning("[EmotionEngine] emotion response is array but empty or invalid, skipping")
                    return False
            if not isinstance(result, dict):
                logger.warning("[EmotionEngine] emotion response is not a dict (got %s), skipping", type(result).__name__)
                return False

            # 解析 layers 数组，过滤无效情绪，最多取前 3 层
            raw_layers = result.get("layers", [])
            valid_layers = [
                {"emotion": l["emotion"], "intensity": float(l.get("intensity", 0.5))}
                for l in raw_layers
                if isinstance(l, dict) and l.get("emotion") in emotion_keys
            ]
            valid_layers.sort(key=lambda x: x["intensity"], reverse=True)
            valid_layers = valid_layers[:3]

            if not valid_layers:
                logger.warning("[EmotionEngine] no valid emotion layers returned, skipping")
                return False

            prev_primary = state.get("primary_emotion")
            prev_layers = state.get("emotion_layers") or []
            state["_prev_primary"] = prev_primary
            state["emotion_layers"] = valid_layers

            # 同步 legacy 字段
            state["primary_emotion"]   = valid_layers[0]["emotion"]
            state["primary_weight"]    = valid_layers[0]["intensity"]
            state["secondary_emotion"] = valid_layers[1]["emotion"] if len(valid_layers) > 1 else None
            state["secondary_weight"]  = valid_layers[1]["intensity"] if len(valid_layers) > 1 else 0.0
            state["tertiary_emotion"]  = valid_layers[2]["emotion"] if len(valid_layers) > 2 else None
            state["tertiary_weight"]   = valid_layers[2]["intensity"] if len(valid_layers) > 2 else 0.0
            state["last_updated"] = time.time()
            # user_sentiment：如果 LLM 返回了就存，否则保持原值不覆盖
            user_sentiment = result.get("user_sentiment", "")
            if user_sentiment:
                state["user_sentiment"] = user_sentiment
            self.save_state(session, state)

            primary = valid_layers[0]["emotion"]
            classifier_reason = result.get("reason") or ""

            # emotion.log：与示例结构一致的一行 JSON
            turn_count = state.get("classifier_call_count", 0)
            old_emotions = [[lay["emotion"], lay["intensity"]] for lay in prev_layers]
            new_emotions = [[lay["emotion"], lay["intensity"]] for lay in valid_layers]
            classifier_results = [
                {"emotion": lay["emotion"], "score": lay["intensity"], "rank": i}
                for i, lay in enumerate(valid_layers)
            ]
            added_emotion = [primary, valid_layers[0]["intensity"]] if primary != prev_primary else None
            prev_weight = prev_layers[0].get("intensity", 0.5) if prev_layers else 0.5
            removed_emotion = [prev_primary, prev_weight] if (prev_primary and primary != prev_primary) else None

            log_emotion_entry({
                "timestamp": time.time(),
                "session_id": session.id,
                "turn_count": turn_count,
                "old_emotions": old_emotions,
                "removed_emotion": removed_emotion,
                "classifier_results": classifier_results,
                "new_emotions": new_emotions,
                "added_emotion": added_emotion,
                "old_primary": prev_primary,
                "new_primary": primary,
                "input_text": msgs_text,
                "result_emotion": primary,
                "classifier_reason": classifier_reason,
            })
            log_classify_call(session.id, result, model_name)
            logger.info(
                "[EmotionEngine] classify session=%s layers=%s",
                session.id, [(l["emotion"], l["intensity"]) for l in valid_layers],
            )

            # 成功：清除 analysis 警告
            from src.utils.engine_warnings import clear_warning
            clear_warning(app, "analysis")

            changed = primary != prev_primary
            if changed:
                try:
                    from src.config.effective_config import get_effective_engine_config
                    energy_cfg = get_effective_engine_config(app, session.profile_id, "energy")
                    if energy_cfg.get("enabled", True) is not False:
                        self.apply_emotion_to_energy(session, primary)
                except Exception:
                    # 不让能量联动影响情绪分类主流程
                    pass
            return changed

        except Exception as e:
            logger.warning("[EmotionEngine] classify failed for %s: %s", session.id, e)
            log_error("emotion", str(e), {"session_id": getattr(session, "id", None)}, traceback_str=traceback.format_exc())
            from src.utils.engine_warnings import set_warning
            set_warning(app, "analysis", f"辅助模型调用失败({type(e).__name__}) — 情绪分类暂停")
            return False

    def emotion_changed(self, session) -> bool:
        """检查最近一次分类后情绪是否变化。"""
        state = self.load_state(session)
        return state.get("primary_emotion") != state.get("_prev_primary")
