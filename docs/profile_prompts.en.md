# Persona JSON field reference

> Onboarding and Settings overview: [GETTING_STARTED.en.md](./GETTING_STARTED.en.md).
> **中文：** [profile_prompts.zh.md](./profile_prompts.zh.md)

Persona files live at `profiles/<profile_id>.json` and define all behavior parameters for one AI persona.

> **Most fields are editable through Settings → Personas without touching JSON. This doc is an advanced field reference for those writing or fine-tuning personas by hand.**

---

## Top-level identity

| Field | Type | Description |
|---|---|---|
| `profile_id` | string | Unique id; must match directory name; letters, digits, underscore only |
| `display_name` | string | Display name for UI and TTS |
| `avatar` | string | Avatar path relative to project root, e.g. `static/avatars/default.png` |

---

## Core prompt fields

| Field | Type | Description |
|---|---|---|
| `base_prompt` | string | Core system prompt (priority 0). Identity, tone, expression rules (800–2000 chars recommended) |
| `style_constraint` | string | Reply style limits: length cap, forbidden patterns, address term. Appended after `base_prompt` (200–400 chars) |
| `code_expression_constraint` | string | Extra rules when discussing code/tech topics (optional) |

---

## emotion_config — Emotion engine

Controls how emotion classification, energy, and affinity affect reply style.

```json
"emotion_config": {
  "enabled": true,
  "default_state": "calm",
  "energy_prompts": { "0": "...", "10": "...", "30": "...", "60": "...", "80": "..." },
  "affinity_prompts": { "-100": "...", "0": "...", "200": "...", "1000": "..." },
  "emotion_zh_descriptions": { "joyful": "恬静的愉悦", ... },
  "emotion_prompts": { "joyful": ["..."], "sad": ["..."], ... }
}
```

| Sub-field | Description |
|---|---|
| `enabled` | Toggle emotion engine (overrides global `engines.emotion.enabled`) |
| `default_state` | Initial emotion key; must exist in `emotion_prompts` |
| `energy_prompts` | Energy → prompt map. Keys are fixed: `'0','10','30','60','80'`. Values are **behavioral directives** (60–150 chars each) specifying reply style at that energy level: length, initiative (start topics or only respond?), vocabulary energy, pacing. Scale from exhausted (0) to peak (80). No scene descriptions |
| `affinity_prompts` | Affinity → prompt map. Keys fixed: `'-100','0','200','400','600','800','1000','1200'`. Values are **behavioral directives** (80–160 chars) in imperative style — explicitly state emotional distance, whether physical contact/intimacy is permitted, vocabulary/tone, and **forbidden behaviors** at this tier. These are hard constraints read directly by the AI; be prescriptive, not descriptive |
| `emotion_zh_descriptions` | emotion key → 4–8 Chinese characters describing the emotion in the character's voice, used for TTS instruct |
| `emotion_prompts` | emotion key → **behavioral directive** array (3–4 items). Tells the AI HOW to speak in this emotional state: tone/register, sentence length, vocabulary, pacing, what to say more/less of. No scene descriptions or third-person observations; each item must be an actionable rule |

**Standard emotion key set** (17 keys, consistent with public release):
`calm` `joyful` `excited` `confident` `gentle` `grateful` `nostalgic` `thoughtful` `dreamy` `concerned` `playful_teasing` `sad` `disappointed` `angry` `sarcastic` `vulnerable` `tired`

---

## memory_config — Memory engine (per-persona overrides)

All fields are optional; omitted fields fall back to `config/app.yaml` global defaults.

```json
"memory_config": {
  "enabled": true,
  "extraction_frequency": 5,
  "extraction_weight_threshold": 0.5,
  "max_facts_in_prompt": 20,
  "recent_days": 3,
  "recent_fact_quota": 5,
  "semantic_fact_quota": 10,
  "semantic_distance": 0.45
}
```

| Sub-field | Description |
|---|---|
| `enabled` | Toggle memory extraction |
| `extraction_frequency` | Extract once every N messages |
| `extraction_weight_threshold` | Confidence threshold (0–1); facts below this are discarded |
| `max_facts_in_prompt` | Max facts injected per request |
| `recent_days` | How many days count as "recent" |
| `recent_fact_quota` | Slot limit for recent facts |
| `semantic_fact_quota` | Slot limit for semantic retrieval facts |
| `semantic_distance` | Vector retrieval distance threshold (lower = stricter) |

---

## reflection_config — Reflection & auxiliary analysis persona (optional)

| Sub-field | Description |
|---|---|
| `custom_prompt` | **Single source**: injected into the reflection LLM as persona context. Also shared with emotion classification and affinity LLM calls (truncated at ~3500 chars). Does **not** include `base_prompt`. Recommended format: open with a ~150-char `【角色要点】你是{name}: {key traits}` block, then first-person introspection instructions written as the character itself. Keep concise — the `thought` output must be ≤30 Chinese chars / ≤20 English words |
| `chat_inject_topic_anchor` | Whether to inject `topic_anchor` into the main chat prompt (default: true) |
| `long_absence_hours` etc. | See engine and segment docs |

If `custom_prompt` is absent, reflection still runs but without persona context; emotion/affinity auxiliary calls will also lack the persona block.

---

## memory_extraction_prompt / memory_day_summary_prompt (top-level fields)

These two fields sit at the top level of the profile JSON (outside `emotion_config`):

| Field | Description |
|---|---|
| `memory_extraction_prompt` | 200–400 chars. Must open with `【角色要点】你是{name}: {key traits}\n\n` (~150 chars), then first-person extraction instructions where the AI extracts memories as itself |
| `memory_day_summary_prompt` | 150–280 chars. Same `【角色要点】` prefix, then first-person diary-style day-summary instruction |

---

## TTS fields

### GPT-SoVITS (per-profile override)

| Field | Description |
|---|---|
| `gpt_sovits_ref_text` | Reference audio transcript |
| `gpt_sovits_ref_audio_path` | Reference audio path (relative to project root) |

### Qwen3-TTS (per-profile override)

| Field | Description |
|---|---|
| `qwen3_tts_ref_audio_path` | Voice Clone reference audio path |
| `qwen3_tts_ref_text` | Voice Clone reference text |
| `qwen3_tts_instruct` | Fixed tone/emotion note appended to auto-instruct |
| `qwen3_tts_voice_description` | VoiceDesign mode: natural-language voice description |
| `qwen3_tts_speaker` | CustomVoice mode: preset voice name (default: Vivian) |

### Kokoro (per-profile override)

| Field | Description |
|---|---|
| `kokoro_voice` | Kokoro voice name |
| `kokoro_lang` | Language code (`zh` / `ja` / `en`) |
| `kokoro_speed` | Speed multiplier |

---

## special_dates (optional)

Trigger context hints on specific dates (birthdays, anniversaries, etc.).

```json
"special_dates": [
  { "month": 12, "day": 25, "label": "Christmas", "hint": "It's Christmas today — say something festive." }
]
```

---

## engine_overrides (optional)

Override global engine parameters for this persona only.

```json
"engine_overrides": {
  "reflection": { "enabled": true, "interval_seconds": 120 },
  "ase": { "enabled": true, "mode": "high" }
}
```

---

## lorebook — World settings (optional)

Injected by the `lorebook` segment (priority 81). Enable/disable in Settings → Persona → Prompt segments → "Lorebook".

### Recommended: `lorebook_ref` + `lorebooks/<id>.json`

Bind in Settings → Persona → Lorebook tab. Multiple personas can share one file.

```json
"lorebook_ref": "st_mychar_abc12345"
```

File format (`lorebooks/<id>.json`):

```json
{
  "lorebook_id": "st_mychar_abc12345",
  "display_name": "Character Lorebook",
  "lorebook_settings": { "scan_turns": 10, "default_entry_mode": "keyword" },
  "entries": [
    { "keys": ["magic"], "content": "[Magic system]...", "enabled": true, "mode": "keyword" },
    { "keys": [], "content": "[World overview]...", "enabled": true, "mode": "constant" }
  ]
}
```

| Entry field | Description |
|---|---|
| `keys` | Trigger keywords (substring match); omit for constant entries |
| `content` | Injected text |
| `enabled` | Whether this entry is active |
| `mode` | `inherit` (use book default) / `keyword` / `constant` |

If the file referenced by `lorebook_ref` is missing, falls back to the inline `lorebook` array inside the profile JSON (legacy).
