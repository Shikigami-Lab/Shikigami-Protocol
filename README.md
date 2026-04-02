<div align="center">

<img src="assets/shikigami_protocol_icon.png" width="120" alt="Shikigami Protocol" />

# Shikigami Protocol

**A local-first AI character companion framework**

*It remembers you. It feels with you. When you go quiet, it reaches out.*

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](https://github.com/Shikigami-Lab/Shikigami-Protocol)
[![Release](https://img.shields.io/github/v/release/Shikigami-Lab/Shikigami-Protocol)](https://github.com/Shikigami-Lab/Shikigami-Protocol/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/Shikigami-Lab/Shikigami-Protocol/total)](https://github.com/Shikigami-Lab/Shikigami-Protocol/releases)

<a href="README_zh.md">简体中文</a>

<br>

[Quick start](#quick-start) · [Example personas](#example-personas) · [Features](#features) · [Discussions](https://github.com/Shikigami-Lab/Shikigami-Protocol/discussions) · [License](#license-community) · [Documentation](#documentation) · [Changelog](CHANGELOG.md)

</div>

> ⚠️ **Project Status**: Currently in Public Beta (v0.9.x). Core architecture is stable and ready to use, but unknown bugs may exist. Feel free to join the [Discussions](https://github.com/Shikigami-Lab/Shikigami-Protocol/discussions) to leave feedback.

Shikigami Protocol is a **local AI character companion framework**. The character remembers you, maintains emotion / energy / affinity state, reflects in the background, and speaks proactively after silence — with optional VLM screen context and emotion-aware TTS.

![Shikigami Protocol UI: Multiple themes — light, dark, and more](assets/readme/ui-themes.png)

We believe AI companions should be both smart and beautiful. The interface ships with multiple built-in themes, customizable sidebars, and a guided onboarding flow for true out-of-the-box readiness.

### 🏗️ Technical Architecture: More than a wrapper

![Shikigami Protocol Core Architecture (Whiteboard Style)](assets/readme/shikigami_architecture_excalidraw.png)

### ✨ Core Highlights

- **Out-of-the-box Desktop App**: Standalone `.exe` with a clean UI—no coding required.
- **Cross-device Web UI**: Runs a local server with a responsive UI. Access from your phone or tablet on the same network — no separate app needed.
- **Emotion & Affinity Engine**: Goes beyond text completion with state machines for mood and fatigue.
- **Proactive Engagement (ASE)**: If you go quiet, the character reflects and speaks up autonomously.
- **Integrated Memory Pipeline**: Built-in fact extraction and vector retrieval. They remember.
- **Persona Evolution**: Memory shapes identity. As shared experience accumulates, the persona quietly reconstructs itself — a *core anchor* keeps the original edge intact so the character grows without losing what makes them them.
- **100% Private**: Local-first architecture. Your chat data never leaves your disk.

---

## 💡 Why Shikigami?

**We're not competing with a generic chat web UI** — we benchmark against **mature RP frontends** (e.g. the SillyTavern ecosystem), **cloud companion apps**, and **agent frameworks**. On the path to *personhood*, many stacks optimize prompts and plugin glue (**skin**); Shikigami bets on **state machines, reflection, proactivity, and an integrated memory pipeline** (**bones**).

| Dimension | Typical RP frontends / cloud companions | Shikigami Protocol |
|:---|:---|:---|
| **Emotion & state** | Often relies on long system prompts to *perform* emotion; cross-turn continuity and decay are left to extensions and luck. | **Emotion × energy × affinity state machine** tied to the chat loop — state persists and decays; not a one-shot mood reset. |
| **Silence & initiative** | If you don't send a message, the thread idles; some "proactive" pushes are scheduled blasts, weakly tied to context. | **Reflection + urgency + ASE** — background monologue and urgency build up; **breaks the silence** when thresholds are met, not a dumb timer. |
| **Memory & cognition** | Often chat history retrieval + vector chunks; quality depends on extensions and tuning. | **Fact extraction + vector retrieval + daily summaries** — pipelines are **built in** and aligned with prompt segments and retrieval policy. |
| **Persona growth** | Static system prompt; no memory feedback loop into the persona itself. | **Persona Evolution**: memories drive periodic `base_prompt` + `style_constraint` reconstruction. A *core anchor* (immutable trait statements) prevents RLHF "customer-service creep"; changelog + one-click rollback built in. |
| **World context** | Lorebooks and manual background are common; time, weather, trends, and screen may not be unified. | **Time / lunar / solar terms, weather, trends** can be injected; optional **VLM** screen context for replies and pre-speech. |
| **Companion tools** | Todos, reminders, and search often come from extensions; how tightly they bind to the persona varies. | **Todos, timers, web search** (`/todo`, `/timer`, `/search`) live in the chat flow — remember, nudge, look things up — **not** desktop automation or multi-step agents. |
| **Data & sovereignty** | Cloud products sit under platform accounts and policies; local-only setups can still sprawl across extensions. | **Local-first**, data on disks you control; **AGPL** — no platform custody of your persona and logs. |

*For technical depth, see [Features](#features) below.*

## ❌ What this is not

- **Not your personal assistant bot.** No desktop control, browser automation, or multi-step agentic workflows. For what *is* included (companion tools vs. not), see the **Companion tools** row above and [Features](#features).
- **Not a stable productivity machine.** The emotion engine means your companion can turn anxious, drained, or withdrawn. If you want a stateless Q&A box to review code, just use ChatGPT.
- **Not a plug-and-play cloud app.** You supply the API keys or local model. Python environment or Docker required.

<a id="quick-start"></a>

## Quick start

### Option 1: 📥 Download the installer (recommended for new users)

Go to the [Releases page](https://github.com/Shikigami-Lab/Shikigami-Protocol/releases/latest) and download the latest `Shikigami Protocol Setup vX.X.X.exe` (Windows). Double-click to install and follow the built-in setup guide. **No Python or Node.js required.**

You'll need an LLM API key (Gemini / OpenAI / Ollama all work) — the onboarding screen will walk you through it.

---

### Option 2: Run from source

**Requirements**

- Python 3.10+ (3.12 recommended)
- Node.js 18+ (Electron desktop only)
- An LLM API — Gemini / OpenAI / Ollama / any OpenAI-compatible endpoint

**Windows (native / Electron)**

```bat
git clone https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
init.bat
launch.bat
```

**macOS / Linux (native / Electron)**

```bash
git clone https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
bash init.sh
bash launch.sh
```

---

### Option 3: Docker (server / NAS)

```bash
git clone https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
cp .env.example .env   # add your API keys
docker compose up -d
# open http://localhost:7788
```

`profiles/`, `models/`, `Voices/`, `config/` are mounted as volumes. Uncomment `deploy.resources` in `docker-compose.yml` for GPU support (requires nvidia-container-toolkit).

---

### API keys

Edit `.env` with at least one LLM key, e.g. `GEMINI_API_KEY`. Match preset names in `config/app.yaml`, or configure via **Settings → LLM** in the UI (writes back to `.env` automatically).

### Pick a persona

**Settings → Personas** — choose `Luna` or `Mochi` to start.

### FAQ

> For complex issues see the [full setup guide](docs/SETUP_FIRST_RUN.en.md).

- **Blank page or can't connect on :7788** — the backend takes 5–15 seconds to start; wait and refresh. If it still fails, check the terminal for the specific error.
- **Invalid API key / no response** — the variable name in `.env` must match the preset name in `config/app.yaml` (e.g. preset `Gemini-2.5` → variable `GEMINI_2_5_API_KEY`).
- **Memory not extracting** — confirm `memory.enabled: true` in `config/app.yaml`; vector retrieval also requires `chromadb` (install via **Settings → Onboarding**).
- **No TTS audio** — `edge_tts` (default) requires an internet connection; local engines (KokoroTTS / GPT-SoVITS) need separate installation and configuration via Onboarding.
- **Electron crashes or won't start on Windows** — make sure you've run `init.bat` first; if it persists, delete the `.venv` folder and run `init.bat` again.
- **Server / NAS deployment** — use the Docker option; data directories are volume-mounted and will survive upgrades.

<a id="example-personas"></a>

## Example personas

Shipped with the repo — no custom card needed to start. Full files: `profiles/example_luna.json` / `profiles/example_luna_en.json`, `profiles/example_mochi.json` / `profiles/example_mochi_en.json`.

---

### Luna — Still water · quiet observer · late-night presence

She doesn't say much. She's been paying attention the whole time.

> "You okay? You've been quieter than usual."
>
> "Just tired, I guess."
>
> "I'm here. Want to talk, or just sit for a bit?"
>
> *(Three hours later, unprompted)* "You're two hours later than usual."

---

### Mochi — Digital nekomata · proud · insufferably clingy

A cat spirit who chose you as her feeder — unilaterally, non-negotiably.

> "You're back."
>
> "Did you miss me?"
>
> "…whatever. I wasn't waiting."
>
> *flicks her tail at you*

---

<a id="features"></a>

## Features

> Advanced reading — skip this on first run.

### 🧠 Memory System (Soul)

![Shikigami Protocol Memory System: fact extraction, vector retrieval, and weight decay](assets/readme/memory-demo.png)

- **Long-term Facts**: Periodic LLM extraction into persistent JSON with Jaccard deduplication.
- **Vector Retrieval**: Hybrid ChromaDB search for precise recall of details, even months later.
- **Midnight Reflection (00:05) — "AI Diary"**:
  - **Personal Journals**: Summarizes daily interactions into an emotional diary.
  - **Consolidation**: Automatically merges scattered fragments into stable user knowledge.
  - **Active Forgetting**: Simulates an Ebbinghaus curve; weights decay daily to keep minds sharp.

### ❤️ Emotion & Affinity (Bones)

![Shikigami Protocol Emotion & Affinity: energy, emotion layers, and relationship tiers](assets/readme/emotion-affinity.png)

- **Emotion Engine**: Real-time state machine affecting tone and TTS sentiment.
- **Energy System**:
  - **Social Burnout**: AI gets tired; conversations drain energy.
  - **Rest & Recovery**: Energy restores slowly while you are offline.
- **Relationship Tiers**: 8 tiers from *Stranger* to *Eternal Bond*, unlocking deeper dialogue.

### 💭 Autonomous Drive (Will)

![Shikigami Protocol Autonomous Drive: inner reflection state and proactive message](assets/readme/autonomous-demo.png)

- **Background Reflection**:
  - **Inner Monologue**: AI thinks and daydreams while you are quiet.
  - **Social Urge**: Decides how much they *want* to talk based on their own thoughts.
- **Proactive Engagement (ASE)**:
  - **Breaking Silence**: Reaches out when they have a thought or you've been gone too long.
  - **Living Presence**: They tease, share ideas, or check in — no longer just a static box.

### 👁️ Visual & Context Awareness

They live in your world, not in a vacuum.

- **Spatiotemporal Resonance**:
  - Feels the 3 AM quiet or a busy Monday morning.
  - Knows your weather and the change of seasons.
  - Global Pulse: Through trend awareness, it knows what's happening on the internet.
- **Shared Vision (VLM)**:
  - **Eyes on You**: Perceives your screen (gaming, coding, browsing).
  - **Live Commentary**: Like a friend sitting nearby, they comment on your screen content.

### 🔧 Companion Tools

Not a corporate bot. A life partner who actually cares.

- **Shared Commitments**:
  - **Natural Reminders**: Set `/todo` or `/timer` casually in chat.
  - **Friendly Nudges**: Reminds you within the conversation, not via system alerts.
- **Seamless Knowledge**:
  - **Live Search**: Use `/search` to pull web data directly into the chat.
  - **Stay Focused**: Never tab out; information flows naturally into the chat.

### 🌱 Persona Evolution

The character that comes back a month later is not the same one you met on day one — and that is by design.

- **Memory-driven reconstruction**: every N memory refreshes, the AI rewrites `base_prompt` + `style_constraint` based on accumulated facts and affinity state. No manual editing required.
- **Core anchor**: a set of immutable trait statements (e.g. *"hides warmth behind distance; never admits she cares first"*) passed as a hard constraint on every rewrite — preventing the RLHF drift that turns characters into polite customer-service bots over time.
- **Original preserved**: the user-authored original is kept read-only, always visible. Evolution writes to a parallel `persona_evolved` field; the original is untouched.
- **Full audit trail**: per-run changelog with change summary, rationale, and one-click rollback to any prior version.
- **User in control**: evolution can be disabled per-persona; the evolved version is manually editable; the core anchor is editable and re-extractable at any time.

### 🎭 Persona System & Community Compatibility

- **SillyTavern import** — `.json` (V1 flat / V2 `chara_card_v2`) and `.png` (tEXt chunk).
- **AI persona autofill** — generates emotion descriptions, reflection config, memory config from `base_prompt` in one click
- **Group chat** — multiple personas in one session, streamed with attribution.
- **Dual-track commands** — NL triggers + `/fact`, `/recall`, `/memory`, `/search`, `/todo`, `/timer`, `/help`

### Core config (`config/app.yaml`)

| Field | Default | Notes |
|---|---|---|
| `default_llm` | `"Gemini-3.0"` | Default LLM preset |
| `default_tts` | `"edge_tts"` | TTS backend |
| `engines.emotion.enabled` | `true` | Emotion engine |
| `engines.affinity.enabled` | `true` | Affinity engine |
| `reflection.enabled` | `false` | Needs secondary model |
| `ase.enabled` | `false` | Proactive speech |
| `memory.enabled` | `true` | Long-term memory |
| `memory.vector_enabled` | `true` | Needs `chromadb` |

### Repository layout

```
shikigami-protocol/
├── server.py              # FastAPI entry point
├── main.js                # Electron main process
├── init.bat / init.sh
├── launch.bat / launch.sh
├── Dockerfile / docker-compose.yml
├── config/app.yaml
├── src/                   # api, core, engines, memory, prompt, tts, tools
├── static/                # Vue 3 SPA frontend
├── profiles/              # example_luna.json, example_mochi.json (+ _en variants)
└── docs/                  # GETTING_STARTED.*, SETUP_FIRST_RUN.*, profile_prompts.*
```

To author your own persona, use `profiles/example_luna.json` or `profiles/example_mochi.json` as a template. Full field reference: [profile_prompts.en.md](docs/profile_prompts.en.md).

<a id="documentation"></a>

## Documentation

| Doc | Purpose |
|---|---|
| [GETTING_STARTED.en.md](docs/GETTING_STARTED.en.md) / [.zh.md](docs/GETTING_STARTED.zh.md) | **Start here** |
| [ARCHITECTURE_REFERENCE.en.md](docs/ARCHITECTURE_REFERENCE.en.md) / [.zh.md](docs/ARCHITECTURE_REFERENCE.zh.md) | Architecture & module boundaries |
| [SETUP_FIRST_RUN.en.md](docs/SETUP_FIRST_RUN.en.md) / [.zh.md](docs/SETUP_FIRST_RUN.zh.md) | Install, init, Docker, troubleshooting |
| [profile_prompts.en.md](docs/profile_prompts.en.md) / [.zh.md](docs/profile_prompts.zh.md) | Persona JSON field reference |

When the app is running, open `/docs-viewer.html?doc=GETTING_STARTED.en.md` directly in the UI.

<a id="discussions"></a>

## 💬 Community & Discussions

Need help, or want to share your custom persona? Join our community:

- [🗣️ GitHub Discussions](https://github.com/Shikigami-Lab/Shikigami-Protocol/discussions) (**Recommended** for Q&A, general chat, and persona sharing)
- [🐛 GitHub Issues](https://github.com/Shikigami-Lab/Shikigami-Protocol/issues) (For bug reports and feature requests only)

<a id="license-community"></a>

## License & community

This repository is licensed under [**AGPL-3.0**](LICENSE).

- **Local / self-hosted use**: free to use, modify, and redistribute under AGPL terms.
- **Network service / SaaS**: if you offer the modified program over a network, you must comply with AGPL source-offer obligations.
- **Contributing**: PRs welcome — demo GIFs/screenshots, example SFW persona cards, translations, bug reports (OS + Python version + logs). Read [CONTRIBUTING.md](CONTRIBUTING.md) first and sign off each commit with `git commit -s` per [DCO 1.1](DCO.md).
- **Security**: Report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

*This section is a summary, not legal advice; the [LICENSE](LICENSE) and [DCO](DCO.md) texts prevail.*

---

<div align="center">

Runs locally — all chat data stays on your device, never on any server. This is an open-source tool, not a hosted AI service; output depends on your configured third-party models. Use must comply with applicable laws and each provider's terms of service. Associated costs and consequences are the user's own responsibility.

</div>
