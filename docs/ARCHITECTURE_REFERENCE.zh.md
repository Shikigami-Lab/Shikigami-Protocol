# 架构参考与模块边界

> **面向读者**：贡献者、架构师、或需要了解项目底层结构的开发者。
> 普通用户上手请看 [GETTING_STARTED.zh.md](./GETTING_STARTED.zh.md)。
> **English:** [ARCHITECTURE_REFERENCE.en.md](./ARCHITECTURE_REFERENCE.en.md)

本文说明：**最少需要哪些文件与环境**才能正常启动并完成一次对话，以及各模块的边界与职责。

---

## 运行环境

| 项目 | 要求 | 说明 |
|---|---|---|
| **Python** | 3.10+ | 必须，用于 `server.py` 及全部后端逻辑 |
| **Node.js** | 18+ | 仅 Electron 桌面版需要；纯浏览器访问可不要 |
| **LLM** | 至少一个可用接口 | `.env` 中配置 API Key，或 `config/app.yaml` 中配置本地端点；否则无法生成回复 |

---

## 必须存在的文件

以下路径缺失时，服务可能启动失败或核心功能不可用：

| 路径 | 用途 | 若缺失 |
|---|---|---|
| `config/app.yaml` | 主配置（端口、LLM/TTS 预设、引擎开关等） | 使用代码内默认值（端口 7788 等）；建议保留 |
| `config/prompts/*.yaml` | 所有 LLM 指令（情绪分类、记忆、反思、VLM 等） | `get_prompt()` 取空串，情绪/记忆/反思逻辑异常；**必须存在** |
| `profiles/` | 人格卡目录 | SessionManager 无任何 session，无法对话 |
| `profiles/<id>.json`（至少一张） | 人格卡 | 无可用人格，侧栏为空，无法发起对话 |

---

## 推荐但非硬性必须

| 路径 | 用途 | 若缺失 |
|---|---|---|
| `config/sessions_meta.json` | 当前选中人格与侧栏顺序 | SessionManager 用第一个扫描到的人格为当前 |
| `.env` | API Key、HF 离线等 | 无 Key 时无法调用 LLM；可在 UI 设置中配置 |
| `profiles/<id>/`（数据目录） | 每人格的对话、情绪状态、记忆等 | 首次使用时由各模块按需创建 |

---

## 核心模块边界

| 模块 | 路径 | 职责边界 |
|---|---|---|
| **API 层** | `src/api/` | ~120 个 REST 端点 + 1 个 WebSocket TTS；路由层，不含业务逻辑 |
| **Prompt Pipeline** | `src/prompt/pipeline.py` + `segments/` | `build_messages()` 汇总所有 segment，合并为**单条 system message** |
| **后台引擎** | `src/core/` | Reflection、ASE、DailyMemoryJob；各自独立 asyncio 循环 |
| **实时引擎** | `src/engines/` | EmotionEngine、AffinityEngine；每 N 轮触发 |
| **记忆系统** | `src/memory/` | SQLite 对话（WAL）、长期事实（JSON）、ChromaDB 向量、日摘要；**无跨模块事务** |
| **LLM 层** | `src/llm/` | 仅 `OpenAICompatProvider`；所有 LLM 调用统一走此入口 |
| **TTS 层** | `src/tts/` | 4 个提供商：Edge / GPT-SoVITS / Qwen3-TTS / Kokoro |
| **工具插件** | `src/tools/` | 待办、计时器、天气、趋势、网页搜索；通过 `registry.py` 注册 |
| **双轨命令** | `src/commands/` | NL triggers + `/commands` 分发；不修改主对话流 |
| **前端** | `static/` | Vue 3 SPA（CDN，非模块）；SSE 接收实时事件 |

**关键不变量**：
- 永远只有**一条 system message**（多 system 会导致 LLM 漂移）
- `session.id == profile_id`（不允许 UUID session）
- 全局 `app.state.last_user_message_time` 需与 session 级别同步更新（防 ASE 误触）

---

## 快速自检清单

- [ ] `config/app.yaml` 存在
- [ ] `config/prompts/*.yaml` 存在
- [ ] `profiles/` 下至少一个 `*.json` 人格卡
- [ ] 至少一个 LLM 可用（`.env` 中 API Key 或 `app.yaml` 本地端点）

满足以上即可达到 minimum starting requirement：服务能起、能选人格、能发一条对话并收到 LLM 回复。
