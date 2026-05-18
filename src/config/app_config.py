import os
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


@dataclass
class AppConfig:
    port: int = 7788
    host: str = "127.0.0.1"
    log_level: str = "info"
    default_llm: str = "custom_api_1"
    default_tts: str = "edge_tts"
    max_history_turns: int = 20
    user_name: str = "用户"  # 人类发言者标识，群聊认用户与 sender 一致
    user_persona: Dict[str, Any] = field(default_factory=dict)  # 主角（用户）全局设定
    llm_presets: Dict[str, Any] = field(default_factory=dict)
    tts_config: Dict[str, Any] = field(default_factory=dict)
    secondary_models: Dict[str, Any] = field(default_factory=dict)
    engines: Dict[str, Any] = field(default_factory=dict)
    memory: Dict[str, Any] = field(default_factory=dict)
    reflection: Dict[str, Any] = field(default_factory=dict)
    ase: Dict[str, Any] = field(default_factory=dict)
    vlm: Dict[str, Any] = field(default_factory=dict)
    stt: Dict[str, Any] = field(default_factory=dict)
    topic_discovery: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str = None) -> "AppConfig":
        from src.utils.paths import get_resource_path
        if path is None:
            path = get_resource_path(os.path.join("config", "app.yaml"))
        
        raw: Dict[str, Any] = {}
        # Fall back to app.yaml.example if app.yaml doesn't exist (e.g. fresh clone without running init)
        if not os.path.exists(path):
            example_path = path.replace("app.yaml", "app.yaml.example")
            if os.path.exists(example_path):
                path = example_path
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning(f"[AppConfig] failed to load {path}: {e}")

        presets: Dict[str, Any] = raw.get("llm_presets", {})

        # Inject API keys from environment variables
        # Convention: <PRESET_NAME_UPPER>_API_KEY
        for name, preset in presets.items():
            env_key = f"{name.upper().replace(' ', '_')}_API_KEY"
            env_val = os.environ.get(env_key, "").strip()
            if env_val:
                preset["api_key"] = env_val

        return cls(
            port=int(raw.get("port", 7788)),
            host=raw.get("host", "127.0.0.1"),
            log_level=raw.get("log_level", "info"),
            default_llm=raw.get("default_llm", "custom_api_1"),
            default_tts=raw.get("default_tts", "edge_tts"),
            max_history_turns=int(raw.get("max_history_turns", 20)),
            user_name=str(raw.get("user_name", "用户")).strip() or "用户",
            user_persona=raw.get("user_persona", {}),
            llm_presets=presets,
            tts_config=raw.get("tts", {}),
            secondary_models=raw.get("secondary_models", {}),
            engines=raw.get("engines", {}),
            memory=raw.get("memory", {}),
            reflection=raw.get("reflection", {}),
            ase=raw.get("ase", {}),
            vlm=raw.get("vlm", {}),
            stt=raw.get("stt", {}),
            topic_discovery=raw.get("topic_discovery", {}),
        )

    def get_analysis_preset(self) -> Optional[Dict[str, Any]]:
        """返回 analysis 辅助模型对应的 LLM preset dict；未启用或 preset 不存在时返回 None。"""
        sec = self.secondary_models.get("analysis", {})
        if not sec.get("enabled"):
            return None
        preset_name = sec.get("preset", "")
        # 特殊值："" = 使用激活模型；"__none__" = 不使用分析模型
        if preset_name == "__none__":
            return None
        if not preset_name:
            return self.get_active_llm_preset()
        return self.llm_presets.get(preset_name)

    def get_engine_config(self, engine: str) -> Dict[str, Any]:
        """返回指定引擎的配置 dict，不存在时返回空 dict。"""
        return self.engines.get(engine, {})

    def get_active_llm_preset(self) -> Dict[str, Any]:
        return self.llm_presets.get(self.default_llm, {})

    def get_llm_preset(self, preset_name: str) -> Dict[str, Any]:
        """按名称返回 LLM preset；不存在时 fallback 到激活 preset。"""
        return self.llm_presets.get(preset_name) or self.get_active_llm_preset()

    def get_tts_voice(self) -> str:
        return (
            self.tts_config.get("edge_tts", {}).get("voice", "zh-CN-XiaoxiaoNeural")
        )

    def get_memory_config(self) -> Dict[str, Any]:
        """返回 memory 节配置，含默认值。"""
        defaults = {
            "enabled": False,
            "vector_enabled": False,
            "extraction_frequency": 5,
            "extraction_weight_threshold": 0.5,
            "max_facts_in_prompt": 8,
            "max_history_turns": 20,
            "day_summary_enabled": False,
            "day_summary_keep_days": 14,
            "day_summary_max_messages": 100,      # 写日记/日摘要时单日最多取多少条对话（超出截断）
            "day_summary_max_conv_chars": 12000,   # 发给模型的对话总文本最大字符数（超出截断）
            "daily_forgetting_enabled": False,
            "daily_decay_factor": 0.998,
            "daily_decay_min_weight": 0.1,
            "daily_reinforcement_enabled": False,
            "daily_run_at_hour": 0,
            "daily_run_at_minute": 5,
            "daily_consolidation_enabled": False,
            "daily_consolidation_after_days": 30,
            "daily_consolidation_weight_below": 0.3,
            "daily_consolidation_batch_max": 15,
            "vector_dedup_distance_threshold": 0.1,  # 写入向量时：距离<此值视为「几乎相同」删旧写新
            "embedding": {
                "provider": "gemini",
                "gemini_model": "text-embedding-004",
                "local_model": "all-MiniLM-L6-v2",
                "device": "auto",  # local 模型设备: auto(优先 cuda) | cuda | cpu
                "offline": False,  # True=仅用缓存不请求 Hugging Face（模型已下载后可设为 true）
            },
        }
        result = {**defaults, **self.memory}
        # Merge embedding sub-dict
        result["embedding"] = {**defaults["embedding"], **self.memory.get("embedding", {})}
        return result

    def get_reflection_config(self) -> Dict[str, Any]:
        """返回 reflection 节配置，含默认值。"""
        defaults = {
            "enabled": False,
            "interval_seconds": 180,
            "interval_min_seconds": 20,
            "interval_max_seconds": 1800,
            "retry_seconds": 60,
            "recent_turns": 10,
            "state_ttl_seconds": 600,
            "idle_throttle_after_seconds": 900,
            "idle_interval_seconds": 600,
        }
        return {**defaults, **self.reflection}

    def get_ase_config(self) -> Dict[str, Any]:
        """返回 ase 节配置，含默认值。"""
        defaults: Dict[str, Any] = {
            "enabled": False,
            "mode": "medium",
            "vlm_mode": "random",
            "modes": {},
            "proactive_hints": {},
        }
        result = {**defaults, **self.ase}
        # Deep-merge modes dict so partial overrides work
        result["modes"] = {**defaults["modes"], **self.ase.get("modes", {})}
        # proactive_hints defaults
        ph = result.get("proactive_hints") or {}
        result["proactive_hints"] = {
            "morning_greeting_enabled": ph.get("morning_greeting_enabled", True),
            "morning_hours": ph.get("morning_hours", [5, 6, 7, 8, 9, 10]),
            "check_in_enabled": ph.get("check_in_enabled", True),
            "check_in_after_silent_seconds": ph.get("check_in_after_silent_seconds", 3600),
            "check_in_probability": ph.get("check_in_probability", 0.5),
        }
        return result

    def get_topic_discovery_config(self) -> Dict[str, Any]:
        """返回 topic_discovery 节配置，含默认值（主动话题发现）。"""
        defaults: Dict[str, Any] = {
            "enabled": True,
            "candidate_cap": 8,
            "recent_used_window": 10,
            "chosen_topic_ttl": 1800,
            "sources": {},
        }
        source_defaults = {
            "trend":               {"enabled": True},
            "conversation_recall": {"enabled": True},
            "user_life":           {"enabled": True},
            "ai_self":             {"enabled": True},
            "random_api": {
                "enabled": False,
                "mode": "builtin",
                "builtin_pool": "icebreaker",
                "http_url": "",
                "http_json_path": "",
            },
        }
        result = {**defaults, **self.topic_discovery}
        merged_sources = {}
        user_sources = self.topic_discovery.get("sources", {}) or {}
        for sid, sdef in source_defaults.items():
            merged_sources[sid] = {**sdef, **(user_sources.get(sid) or {})}
        result["sources"] = merged_sources
        return result

    def get_vlm_config(self) -> Dict[str, Any]:
        """返回 vlm 节配置，含默认值。"""
        defaults: Dict[str, Any] = {
            "enabled": False,
            "model_preset": "Gemini-3.0",
            "for_chat": True,
            "for_ase": True,
            "ase_wait_seconds": 3,
        }
        return {**defaults, **self.vlm}

    def get_reflection_model_preset(self) -> Optional[Dict[str, Any]]:
        """返回自省用 LLM preset；先试 primary，失败再试 fallback，均失败返回 None。"""
        sec = self.secondary_models.get("reflection", {})
        if not sec.get("enabled", True):
            return None
        primary_name = sec.get("primary_preset", "")
        # 特殊值："__none__"=忽略该槽位；"__active__"=使用激活模型
        if primary_name == "__active__":
            return self.get_active_llm_preset()
        if primary_name and primary_name != "__none__" and primary_name in self.llm_presets:
            return self.llm_presets[primary_name]
        fallback_name = sec.get("fallback_preset", "")
        if fallback_name == "__active__":
            return self.get_active_llm_preset()
        if fallback_name and fallback_name != "__none__" and fallback_name in self.llm_presets:
            return self.llm_presets[fallback_name]
        return None

    def get_reflection_model_names(self) -> tuple:
        """返回 (primary_preset_name, fallback_preset_name)。"""
        sec = self.secondary_models.get("reflection", {})
        return (sec.get("primary_preset", ""), sec.get("fallback_preset", ""))

    def get_stt_config(self) -> Dict[str, Any]:
        """返回 STT 配置，含默认值（SenseVoice / sherpa-onnx）。"""
        defaults = {
            "enabled": True,
            "model_path": "",
            "language": "zh",  # zh | en | ja | ko | auto
            "num_threads": 4,
            "min_chars": 2,    # 去标点后少于此字符数时视为噪声丢弃
        }
        return {**defaults, **self.stt}

    def get_active_tts_config(self) -> Dict[str, Any]:
        """Return a config dict suitable for get_tts_provider()."""
        tts_type = self.default_tts
        cfg: Dict[str, Any] = {"type": tts_type}
        if tts_type == "edge_tts":
            cfg["voice"] = self.get_tts_voice()
        elif tts_type == "gpt_sovits":
            cfg.update(self.tts_config.get("gpt_sovits", {}))
            cfg["type"] = "gpt_sovits"   # ensure type key is preserved
        elif tts_type == "qwen3_tts":
            cfg.update(self.tts_config.get("qwen3_tts", {}))
            cfg["type"] = "qwen3_tts"
        return cfg
