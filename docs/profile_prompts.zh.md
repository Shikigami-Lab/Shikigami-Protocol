# 人物卡 JSON 字段说明

> 上手路线图与设置页导览见 [GETTING_STARTED.zh.md](./GETTING_STARTED.zh.md)。**English:** [profile_prompts.en.md](./profile_prompts.en.md)

人物卡文件位于 `profiles/<profile_id>.json`，定义了单个 AI 人格的全部行为参数。

---

## 顶层标识字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `profile_id` | string | 唯一标识，与目录名一致，仅允许字母/数字/下划线 |
| `display_name` | string | 显示名称，用于 UI 和 TTS |
| `avatar` | string | 头像路径（相对于项目根），如 `static/avatars/default.png` |

---

## 核心 Prompt 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `base_prompt` | string | 人格核心系统提示，priority=0 注入。包含身份、性格、表达原则等（800–2000 字为宜） |
| `style_constraint` | string | 回复风格约束（字数限制、禁止行为等），追加到 base_prompt 后（200–400 字为宜） |
| `code_expression_constraint` | string | 涉及代码/技术话题时的特殊表达规则（可选）|

---

## emotion_config — 情绪引擎

控制情绪分类、能量与好感度对回复风格的影响。

```json
"emotion_config": {
  "enabled": true,
  "default_state": "calm",
  "energy_prompts": { "0": "...", "30": "...", "60": "...", "80": "..." },
  "affinity_prompts": { "-100": "...", "0": "...", "200": "...", "1000": "..." },
  "emotion_zh_descriptions": { "joyful": "恬静的愉悦", ... },
  "emotion_prompts": { "joyful": ["..."], "sad": ["..."], ... }
}
```

| 子字段 | 说明 |
|---|---|
| `enabled` | 是否启用情绪引擎（覆盖全局 `engines.emotion.enabled`）|
| `default_state` | 初始情绪状态 key，对应 `emotion_prompts` 中的键 |
| `energy_prompts` | 能量值 → prompt 映射。key 为数字字符串（阈值，固定为 `'0','10','30','60','80'`），值为 60–150 字的**行为指令**，规定该能量等级下的回复风格（长短、主动性、措辞活力、节奏）。从枯竭(0)到满血(80)递进。禁止写场景描述 |
| `affinity_prompts` | 好感度 → prompt 映射。key 固定为 `'-100','0','200','400','600','800','1000','1200'`，值为 80–160 字的**行为指令**（命令式写法），明确规定该段位下的情感距离、是否允许肢体接触/亲密、用词语气、**禁止行为**。这是 AI 直接读取的硬约束，需具体而非描述性 |
| `emotion_zh_descriptions` | 情绪 key → 中文描述（4–8 汉字，角色视角），用于 TTS instruct 构建 |
| `emotion_prompts` | 情绪 key → **行为指令**数组（3–4 条），告诉 AI 在该情绪下**如何说话**：语气/语域、句子长短、用词倾向、节奏、多说/少说什么。禁止写场景描述或第三人称旁白，每条必须是可执行规则 |

**标准情绪键集**（与公开发行版一致，共 17 个）：
`calm` `joyful` `excited` `confident` `gentle` `grateful` `nostalgic` `thoughtful` `dreamy` `concerned` `playful_teasing` `sad` `disappointed` `angry` `sarcastic` `vulnerable` `tired`

---

## memory_config — 记忆引擎（人格级覆盖）

所有字段均可选，未填则使用 `config/app.yaml` 中的全局默认值。

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

| 子字段 | 说明 |
|---|---|
| `enabled` | 记忆提取开关 |
| `extraction_frequency` | 每 N 条消息触发一次自动提取 |
| `extraction_weight_threshold` | 提取置信度阈值（0–1），低于此值不写入 |
| `max_facts_in_prompt` | 每次最多注入多少条事实 |
| `recent_days` | 多少天内算「近期」事实 |
| `recent_fact_quota` | 近期槽位上限 |
| `semantic_fact_quota` | 语义检索槽位上限 |
| `semantic_distance` | 向量检索距离阈值（越小越严格）|

---

## reflection_config — 自省与辅助分析人设（可选）

| 子字段 | 说明 |
|---|---|
| `custom_prompt` | **单一来源**：注入自省 LLM 的「人设要点 + 自省规则」；与同人格的情感分类、好感度 LLM 调整共用同一段文字（`src/utils/persona_context.py` 读取，默认最长约 3500 字后截断）。**不含** `base_prompt`。格式建议：以 `【角色要点】你是{name}: {key traits}` 开头（约 150 字），后接第一人称自省说明（「此刻你独自思考……」）。`thought` 输出须 ≤30 汉字 / ≤20 英文单词，指令需保持简洁 |
| `chat_inject_topic_hint` | 是否在主对话注入 `topic_hint`（默认 true）|
| `long_absence_hours` 等 | 见引擎与 segment 文档 |

未填写 `custom_prompt` 时，自省仍运行但缺少人设段；情感/好感辅助调用也不会附带该块。

---

## memory_extraction_prompt / memory_day_summary_prompt（顶层字段）

这两个字段在 `emotion_config` 之外，直接位于 profile JSON 顶层：

| 字段 | 说明 |
|---|---|
| `memory_extraction_prompt` | 200–400 字。须以 `【角色要点】你是{name}: {key traits}\n\n` 开头（约 150 字），后接第一人称提取说明（「你是{name}的潜意识，从对话中提取关于主人的重要事实，以第一人称记录如「我记得…」」）|
| `memory_day_summary_prompt` | 150–280 字。须以同样的 `【角色要点】` 前缀开头，后接第一人称日记式总结说明 |

---

## TTS 字段

### GPT-SoVITS（per-profile 覆盖）

| 字段 | 说明 |
|---|---|
| `gpt_sovits_ref_text` | 参考音频对应文本 |
| `gpt_sovits_ref_audio_path` | 参考音频路径（相对于项目根）|

### Qwen3-TTS（per-profile 覆盖）

| 字段 | 说明 |
|---|---|
| `qwen3_tts_ref_audio_path` | Voice Clone 参考音频路径 |
| `qwen3_tts_ref_text` | Voice Clone 参考文本 |
| `qwen3_tts_instruct` | 固定语气/情感说明（追加到自动 instruct 后）|
| `qwen3_tts_voice_description` | VoiceDesign 模式：音色自然语言描述 |
| `qwen3_tts_speaker` | CustomVoice 模式：预设音色名（默认 Vivian）|

### Kokoro（per-profile 覆盖）

| 字段 | 说明 |
|---|---|
| `kokoro_voice` | Kokoro 音色名 |
| `kokoro_lang` | 语言代码（`zh` / `ja` / `en`）|
| `kokoro_speed` | 语速倍率 |

---

## special_dates — 特殊日期（可选）

触发特定日期的情境提示，如生日、纪念日。

```json
"special_dates": [
  { "month": 12, "day": 25, "label": "圣诞节", "hint": "今天是圣诞节，可以说点应景的话。" }
]
```

---

## engine_overrides — 引擎参数覆盖（可选）

覆盖全局引擎参数，仅对本人格生效。

```json
"engine_overrides": {
  "reflection": { "enabled": true, "interval_seconds": 120 },
  "ase": { "enabled": true, "mode": "high" }
}
```

---

## lorebook — 世界设定（可选）

由 `lorebook` segment（priority=81）注入；**是否启用**在设置 → 人格 → Prompt 增强段落 → 「世界书」中勾选。

### 推荐：`lorebook_ref` + 项目目录 `lorebooks/<id>.json`

在设置 → 人格 → 世界书子页绑定。多个人格可共用同一文件。

```json
"lorebook_ref": "st_mychar_abc12345"
```

文件格式（`lorebooks/<id>.json`）：

```json
{
  "lorebook_id": "st_mychar_abc12345",
  "display_name": "《角色名》世界书",
  "lorebook_settings": { "scan_turns": 10, "default_entry_mode": "keyword" },
  "entries": [
    { "keys": ["魔法"], "content": "【魔法体系】...", "enabled": true, "mode": "keyword" },
    { "keys": [], "content": "【总设定】...", "enabled": true, "mode": "constant" }
  ]
}
```

| 条目字段 | 说明 |
|---|---|
| `keys` | 触发关键词（子串匹配）；常驻时可省略 |
| `content` | 注入正文 |
| `enabled` | 是否启用 |
| `mode` | `inherit`（继承书本默认）/ `keyword` / `constant` |

若 `lorebook_ref` 指向的文件缺失，回退到人格 JSON 内嵌 `lorebook`（遗留方式）。
