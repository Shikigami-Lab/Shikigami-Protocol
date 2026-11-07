"""Global task instructions for memory extraction, review, and diaries."""

from src.config.prompt_loader import get_prompt

TASK_PROMPT_KEYS = {
    "extraction_prompt": "memory.extraction_task",
    "day_summary_prompt": "memory.day_summary_task",
    "review_prompt": "memory.review_task",
}


def memory_prompt_settings(memory: dict) -> dict:
    stored = memory.get("task_prompts")
    stored = stored if isinstance(stored, dict) else {}
    overrides = {}
    for key in TASK_PROMPT_KEYS:
        value = stored.get(key)
        overrides[key] = value if isinstance(value, str) and value.strip() else None
    return {
        "defaults": {key: get_prompt(template).strip() for key, template in TASK_PROMPT_KEYS.items()},
        "overrides": overrides,
    }


def memory_task_prompt(memory: dict, key: str) -> str:
    settings = memory_prompt_settings(memory)
    return settings["overrides"][key] or settings["defaults"][key]
