# Architecture reference & module boundaries

> **Audience**: contributors, architects, and developers who need to understand the project's underlying structure.
> For end-user onboarding, see [GETTING_STARTED.en.md](./GETTING_STARTED.en.md).
> **中文：** [ARCHITECTURE_REFERENCE.zh.md](./ARCHITECTURE_REFERENCE.zh.md)

This document covers: the **minimum files and environment** needed for the service to start and complete one conversation, plus module boundary reference.

---

## Runtime environment

| Item | Requirement | Notes |
|---|---|---|
| **Python** | 3.10+ | Required; runs `server.py` and all backend logic |
| **Node.js** | 18+ | Only for Electron desktop; browser-only mode doesn't need it |
| **LLM** | At least one reachable endpoint | Set API key in `.env` or local endpoint in `config/app.yaml`; without it no replies can be generated |

---

## Required files

Missing any of these may cause startup failure or break core functionality:

| Path | Purpose | If missing |
|---|---|---|
| `config/app.yaml` | Main config (port, LLM/TTS presets, engine toggles, etc.) | Code defaults apply (port 7788, etc.); recommended to keep |
| `config/prompts/*.yaml` | All LLM instructions (emotion classification, memory, reflection, VLM, etc.) | `get_prompt()` returns empty string; emotion/memory/reflection logic breaks. **Must exist** |
| `profiles/` | Persona card directory | SessionManager has no sessions; no chat possible |
| `profiles/<id>.json` (at least one) | Persona card | No available persona; sidebar empty; no chat possible |

---

## Recommended but not strictly required

| Path | Purpose | If missing |
|---|---|---|
| `config/sessions_meta.json` | Currently selected persona and sidebar order | SessionManager uses the first scanned persona as current |
| `.env` | API keys, HF offline flag, etc. | No LLM calls possible without keys; configure via UI Settings |
| `profiles/<id>/` (data dir) | Per-persona conversation, emotion state, memory, etc. | Created on first use by each module |

---

## Core module boundaries

| Module | Path | Responsibility boundary |
|---|---|---|
| **API layer** | `src/api/` | ~120 REST endpoints + 1 WebSocket TTS; routing only, no business logic |
| **Prompt pipeline** | `src/prompt/pipeline.py` + `segments/` | `build_messages()` collects all segments, merges into **one system message** |
| **Background engines** | `src/core/` | Reflection, ASE, DailyMemoryJob; each runs its own asyncio loop |
| **Realtime engines** | `src/engines/` | EmotionEngine, AffinityEngine; triggered every N turns |
| **Memory system** | `src/memory/` | SQLite conversations (WAL), long-term facts (JSON), ChromaDB vectors, daily summaries; **no cross-module transactions** |
| **LLM layer** | `src/llm/` | Only `OpenAICompatProvider`; all LLM calls go through this single entry point |
| **TTS layer** | `src/tts/` | 4 providers: Edge / GPT-SoVITS / Qwen3-TTS / Kokoro |
| **Tool plugins** | `src/tools/` | Todos, timers, weather, trends, web search; registered via `registry.py` |
| **Dual-track commands** | `src/commands/` | NL triggers + `/commands` dispatch; does not modify the main chat flow |
| **Frontend** | `static/` | Vue 3 SPA (CDN, non-module); receives realtime events via SSE |

**Key invariants**:
- Always exactly **one system message** (multiple system messages cause LLM drift)
- `session.id == profile_id` (no random UUID sessions)
- Global `app.state.last_user_message_time` must be updated in sync with session-level time (prevents ASE mis-triggering on profile switch)

---

## Quick self-check

- [ ] `config/app.yaml` exists
- [ ] `config/prompts/*.yaml` exists
- [ ] At least one `*.json` persona card in `profiles/`
- [ ] At least one LLM reachable (API key in `.env` or local endpoint in `app.yaml`)

Satisfying the above reaches the minimum starting requirement: service starts, persona is selectable, one message can be sent and an LLM reply received.
