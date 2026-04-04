"""AffinityEngine — 管理 profiles/<id>/affinity_state.json。

纯 LLM 驱动，无本地规则层。
每 N 条消息触发一次 LLM 调用（N = engines.affinity.llm_adjust_frequency）。
"""
import json
import logging
import os
import time
import traceback
from string import Template
from typing import Any, Dict, List, Optional, Tuple

from src.config.prompt_loader import get_prompt, get_locale, render
from src.utils.persona_context import get_persona_context_for_secondary_llm
from src.utils.debug_logger import log_error

logger = logging.getLogger(__name__)

_AFFINITY_LEVELS: List[Tuple[float, float, str]] = [
    (float("-inf"), 0,    "讨厌"),
    (0,    200,  "陌生"),
    (200,  400,  "初识"),
    (400,  600,  "熟悉"),
    (600,  800,  "亲密朋友"),
    (800,  1000, "非常亲密"),
    (1000, 1200, "灵魂伴侣"),
    (1200, float("inf"), "永恒羁绊"),
]

_LEVEL_DESCRIPTIONS: Dict[str, str] = {
    "讨厌":   "保持距离，礼貌但冷淡，不主动",
    "陌生":   "温和、专业，不主动",
    "初识":   "友善，耐心，逐渐建立信任",
    "熟悉":   "温暖，主动关心，偶尔轻松开玩笑",
    "亲密朋友": "自然亲近，可撒娇，有情感共鸣",
    "非常亲密": "深度共情，高情感浓度，知心之交",
    "灵魂伴侣": "完全开放，心有灵犀，无需多言",
    "永恒羁绊": "无条件理解，命运般的陪伴",
}

_MAX_AFFINITY = 1400.0
_MIN_AFFINITY = float("-inf")   # 下限由 _AFFINITY_LEVELS 定义，不人为截断

_DEFAULT_STATE: Dict[str, Any] = {
    "affinity":          100.0,
    "status":            "陌生",
    "total_interactions": 0,
    "last_interaction":  0.0,
    "milestones_unlocked": [],
    "llm_call_count":    0,
}


def _resolve_status(affinity: float) -> str:
    for lo, hi, label in _AFFINITY_LEVELS:
        if lo <= affinity < hi:
            return label
    return "永恒羁绊"


class AffinityEngine:
    """好感度引擎（纯 LLM 驱动）。"""

    def _state_path(self, session) -> str:
        return os.path.join(session.storage_root, "affinity_state.json")

    def load_state(self, session) -> Dict[str, Any]:
        path = self._state_path(session)
        if not os.path.exists(path):
            state = dict(_DEFAULT_STATE)
            state["last_interaction"] = time.time()
            return state
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in _DEFAULT_STATE.items():
                data.setdefault(k, v)
            return data
        except Exception as e:
            logger.warning("[AffinityEngine] load_state failed for %s: %s", session.id, e)
            state = dict(_DEFAULT_STATE)
            state["last_interaction"] = time.time()
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
            logger.warning("[AffinityEngine] save_state failed for %s: %s", session.id, e)

    def increment_interaction(self, session) -> None:
        """每条用户消息调用：计数 + 更新时间戳。不调 LLM。"""
        state = self.load_state(session)
        state["total_interactions"] = state.get("total_interactions", 0) + 1
        state["llm_call_count"] = state.get("llm_call_count", 0) + 1
        state["last_interaction"] = time.time()
        self.save_state(session, state)

    async def maybe_llm_adjust(
        self,
        session,
        recent_turns: List[Dict[str, str]],
        app,
    ) -> None:
        """每 N 条消息触发一次 LLM 好感度调整。"""
        state = self.load_state(session)
        count = state.get("llm_call_count", 0)
        from src.config.effective_config import get_effective_engine_config
        engine_cfg = get_effective_engine_config(app, session.profile_id, "affinity")
        if engine_cfg.get("enabled", True) is False:
            return
        freq = engine_cfg.get("llm_adjust_frequency", 5)
        delta_clamp = float(engine_cfg.get("delta_clamp", 15.0))

        if count == 0 or count % freq != 0:
            return

        await self._do_llm_adjust(session, state, recent_turns, delta_clamp, app)

    async def _do_llm_adjust(
        self,
        session,
        state: Dict[str, Any],
        recent_turns: List[Dict[str, str]],
        delta_clamp: float,
        app,
    ) -> None:
        """调用 analysis LLM 计算好感度 delta。"""
        import time as _time
        from src.llm.registry import get_provider_for_role
        from src.utils.debug_logger import (
            log_engine_update, log_affinity_entry,
            log_secondary_llm_call, log_secondary_llm_response,
        )

        provider = get_provider_for_role("analysis")
        if provider is None:
            logger.debug("[AffinityEngine] no analysis provider, skipping llm_adjust")
            from src.utils.engine_warnings import set_warning
            set_warning(app, "analysis", "辅助模型未配置/未启用 — 情绪分类与好感调整暂停")
            return

        affinity = state.get("affinity", 100.0)
        status = state.get("status", "陌生")

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
                profile = {}

        # 构造对话摘要（认用户用 sender）
        user_name = getattr(getattr(app, "state", None) and getattr(app.state, "config", None), "user_name", "用户") or "用户"
        def _label(m):
            s = (m.get("sender") or "").strip()
            return "用户" if (s == user_name or (not s and m.get("role") == "user")) else (s or "AI")
        turns_text = "\n".join(
            f"[{_label(m)}]: {m['content']}"
            for m in recent_turns
            if m.get("content")
        )
        core = Template(get_prompt("affinity.adjustment")).safe_substitute(
            affinity=f"{affinity:.1f}",
            status=status,
            MAX_AFFINITY=f"{_MAX_AFFINITY:.0f}",
            delta_clamp=delta_clamp,
            turns_text=turns_text,
        )
        persona_ctx = get_persona_context_for_secondary_llm(profile)
        if persona_ctx:
            preamble = render(
                "affinity.analysis_shared_context",
                locale=get_locale(),
                persona_context=persona_ctx,
            ).strip()
            prompt = f"{preamble}\n\n{core}" if preamble else core
        else:
            prompt = core
        messages = [{"role": "user", "content": prompt}]
        model_name = getattr(provider, "model", "unknown")

        log_secondary_llm_call(
            role="affinity",
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
                        logger.warning("[AffinityEngine] stream_chat 失败，重试 session=%s: %s",
                                       session.id, _e)
                        import asyncio as _asyncio
                        await _asyncio.sleep(3.0)
            if _llm_exc:
                raise _llm_exc

            log_secondary_llm_response(
                role="affinity",
                response=response_text,
                model=model_name,
                session_id=session.id,
                duration_ms=int((_time.time() - _t0) * 1000),
            )

            raw = response_text.strip()
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()
            if not raw or raw.startswith("[LLM Error:"):
                logger.warning("[AffinityEngine] llm_adjust empty or error response for %s", session.id)
                return
            # Strip leading '+' from numbers — JSON spec forbids "+5.0" but LLMs emit it
            import re as _re
            raw = _re.sub(r':\s*\+(\d)', r': \1', raw)
            try:
                result = json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning("[AffinityEngine] llm_adjust invalid JSON for %s: %s raw=%.100r", session.id, e, raw)
                log_error("affinity", str(e), {"session_id": getattr(session, "id", None), "raw_preview": raw[:200]}, traceback_str=traceback.format_exc())
                from src.utils.engine_warnings import set_warning
                set_warning(app, "analysis", f"辅助模型返回非 JSON — 好感调整跳过")
                return

            delta = float(result.get("delta", 0.0))
            delta = max(-delta_clamp, min(delta_clamp, delta))
            reason = result.get("reason", "")

            new_affinity = min(affinity + delta, _MAX_AFFINITY)
            new_status = _resolve_status(new_affinity)

            state["affinity"] = round(new_affinity, 2)
            state["status"] = new_status
            self.save_state(session, state)

            turn_count = state.get("llm_call_count", 0)
            turns_text = "\n".join(
                f"[{_label(m)}]: {m['content']}"
                for m in recent_turns
                if m.get("content")
            )
            # 之前/之后的好感度等级描述（用于日志可读性）
            old_status_description = _LEVEL_DESCRIPTIONS.get(status, "")
            new_status_description = _LEVEL_DESCRIPTIONS.get(new_status, "")

            log_affinity_entry({
                "timestamp": time.time(),
                "session_id": session.id,
                "turn_count": turn_count,
                # 之前的好感度与描述
                "old_affinity": affinity,
                "old_status": status,
                "old_status_description": old_status_description,
                # 输入给 LLM 分析的文本
                "input_text": turns_text,
                # LLM 决定的好感度加减与原因
                "delta": delta,
                "reason": reason,
                # 之后的好感度与描述
                "new_affinity": round(new_affinity, 2),
                "new_status": new_status,
                "new_status_description": new_status_description,
                "result_affinity": round(new_affinity, 2),
                "result_status": new_status,
            })
            log_engine_update(
                engine="affinity",
                session_id=session.id,
                delta={"delta": delta, "reason": reason},
            )
            logger.info(
                "[AffinityEngine] llm_adjust session=%s delta=%.1f affinity=%.1f status=%s reason=%s",
                session.id, delta, new_affinity, new_status, reason,
            )
            # 成功：清除 analysis 警告
            from src.utils.engine_warnings import clear_warning
            clear_warning(app, "analysis")

        except Exception as e:
            logger.warning("[AffinityEngine] llm_adjust failed for %s: %s", session.id, e)
            log_error("affinity", str(e), {"session_id": getattr(session, "id", None)}, traceback_str=traceback.format_exc())
            from src.utils.engine_warnings import set_warning
            set_warning(app, "analysis", f"辅助模型调用失败({type(e).__name__}) — 好感调整暂停")

    @staticmethod
    def get_level_description(status: str) -> str:
        return _LEVEL_DESCRIPTIONS.get(status, "")
