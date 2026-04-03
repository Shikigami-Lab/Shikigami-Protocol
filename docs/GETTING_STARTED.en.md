# Getting started

> Environment setup (Python, Node, `init.bat`): [SETUP_FIRST_RUN.en.md](./SETUP_FIRST_RUN.en.md).
> Persona JSON fields: [profile_prompts.en.md](./profile_prompts.en.md).
> **中文：** [GETTING_STARTED.zh.md](./GETTING_STARTED.zh.md)

**Using a release installer (.exe / .dmg / .AppImage)? No Python needed — just follow the two steps below. Running from source? Read [SETUP_FIRST_RUN.en.md](./SETUP_FIRST_RUN.en.md) first.**

---

## Minimum steps to start chatting

| Step | What to do | Where |
|:---:|:---|:---|
| **1** | Configure at least **one chat model** (cloud API key or local base URL) | Settings → **LLM** |
| **2** | Have at least **one persona card** (use the AI Wizard to generate one from a description) | Settings → **Personas** |

Steps **1 + 2** are all you need to start chatting in the main window.

> Environment not set up yet? See [SETUP_FIRST_RUN.en.md](./SETUP_FIRST_RUN.en.md).
> Can't reach Gemini / OpenAI from your network? See the **Proxy** section below.

---

## Proxy (restricted networks)

> Most users can skip this section. Only needed if your network cannot reach Gemini / OpenAI or other cloud APIs directly.

The backend is a **Python process** — "the browser can use a VPN" does not mean Python can too.

**Recommended**:

1. Open **Settings → System**.
2. Enable **Use proxy**, enter: `http://127.0.0.1:<port>` (find the port in your proxy app's HTTP / Mixed Port setting).
3. **Save** and **restart** the app (to apply the env variable).

If you're unsure of the port: check the proxy app's local HTTP port setting, or run `netstat -ano | findstr LISTENING` in CMD and look for common proxy ports (`7890`, `10808`, etc.) on `127.0.0.1`.

You can also edit **`.env`** directly to set `HTTP_PROXY` / `HTTPS_PROXY` — same effect as the Settings page.

---

## Settings tabs overview

| Tab | Purpose |
|:---|:---|
| **Onboarding** | **Step-by-step guide**: checklist, network, memory/TTS/STT; companion behavior points to settings — details in the “Emotion, energy…” section below. |
| **Personas** | Persona card list, edit, **AI Wizard** generation, sidebar order, per-persona memory / engine overrides; **Prompt Editing** sub-tab includes Persona Evolution |
| **LLM** | LLM preset management, API keys, local OpenAI-compatible URLs, auxiliary models (emotion / affinity) |
| **Memory** | Long-term facts, vector memory toggle, embedding model (cloud Gemini or local BGE), diary and forgetting preview |
| **TTS** | Voice engines: Edge / GPT-SoVITS / Kokoro / Qwen3-TTS |
| **Behavior / Reflection** | Character inner monologue, proactive speech configuration |
| **VLM** | Screen / visual understanding: model and screenshot strategy |
| **Tools** | Todos, timers, weather, trends, web search — plugin toggles and config |
| **System** | Theme, proxy, HF mirror, offline toggle |

Persona cards can also configure emotion/energy/affinity prompts, memory strategy, and TTS reference audio — see [profile_prompts.en.md](./profile_prompts.en.md) for field reference.

---

## Emotion, energy, affinity, reflection & ASE

These features go beyond plain chat: the persona has **mood, stamina, long-term rapport**, can **reflect in the background**, and may **message you first** after silence. Defaults work without editing prompts; tune below if you want clingier, calmer, or more “tool-like” behavior.

### Emotion engine

- **What it does**: Classifies layered emotions from dialogue, shaping reply tone (and TTS instruct if used).
- **Where**: **Settings → Personas → Behavior** toggles the engine; per-emotion **rules** live in the persona emotion prompt areas (`emotion_prompts`, `emotion_zh_descriptions`). See [profile_prompts.en.md](./profile_prompts.en.md) → `emotion_config`.

### Energy system

- **What it does**: Chat drains energy; it recovers over offline/wall-clock time. Bands map to “more/less talkative” via `energy_prompts`.
- **Where**: Defaults in `config/app.yaml` → `engines.energy`; per-persona energy behavior in **Personas → Behavior**; band texts in **energy prompt** overrides (write **how to speak**, not scene narration).

### Affinity engine

- **What it does**: Adjusts an affinity score from recent dialogue (tiers from stranger to bonded). `affinity_prompts` set distance, tone, and boundaries per tier.
- **Where**: **Personas → Behavior**; auxiliary LLM in **LLM** settings; tier text in **affinity prompts** on the persona. See `profile_prompts.en.md`.

### Reflection

- **What it does**: Background LLM runs produce short inner state (e.g. topic hints) for chat and ASE. **Uses tokens on a schedule.**
- **Where**: Global toggle and intervals in **Reflection / ASE** (or Behavior); **LLM → reflection model**; per-persona `reflection_config` under **Persona reflection** settings. Prefer a **cheap or local** model. Save segments as the UI indicates.

### Proactive speech (ASE)

- **What it does**: Under silence, urgency, cooldown, and daily caps, the persona may **send a proactive line**. Can combine with tools/trends/VLM.
- **Where**: ASE toggle and **mode** (low/medium/high/game/focus) in settings; timing thresholds largely in `config/app.yaml` → `ase.modes`; screenshot behavior with **VLM** options.

**Tips**: Stabilize base persona before enabling ASE. If too chatty, raise urgency threshold or use **low** mode first.

### Persona Evolution

- **What it does**: Every N conversation turns (same counter as emotion and affinity engines), the AI quietly rewrites `base_prompt` and `style_constraint` — nudged by accumulated facts and affinity state. The persona grows through shared experience rather than being frozen at the first draft forever.
- **Core anchor**: You (or the AI's "Re-extract" button) write a few semicolon-separated immutable traits, e.g. `hides warmth behind distance; never admits she cares first; can't be pushed around`. These are passed as a hard constraint on every rewrite, guarding against the RLHF drift that turns every character into a polite, soulless chatbot.
- **Where**: **Settings → Personas → Prompt Editing**, in the **Persona Evolution** collapsible below Style Constraint:
  - **Enable toggle** (off by default) and **trigger interval** (every N conversation turns, default 200 — uses the same counter as the emotion/affinity engines).
  - **Re-extract**: let the AI distill a core anchor from the current persona text — edit it afterwards as you like.
  - **Original persona (read-only)**: your hand-written original, never touched by evolution, always visible for comparison.
  - **Current evolved version**: the live persona; editable by hand and auto-saved on change.
  - **Evolution log**: per-run change summary and rationale; click **Roll back** to restore any prior version.
- **Tips**: Let the persona accumulate a few dozen turns of memory before enabling. If you've crafted a very precise persona and don't want autonomous rewrites, leave it off — the anchor and changelog are still useful as read-only context.

---

## Optional enhancements

### Memory & vector retrieval

- **Cloud embedding (Gemini etc.)**: configure the API key in LLM settings; vector memory uses the cloud endpoint.
- **Local vector retrieval** (no cloud key): download a BGE-small or similar model to `models/` via the Onboarding tab (HF mirror / ModelScope), then set the embedding provider to local in Memory settings.
- If `HF_HUB_OFFLINE=1` is set, online downloads will fail — disable offline mode or provide model files manually.

### Voice input (STT)

In the Onboarding tab, select an STT engine and download the model:
- **SenseVoice** (ONNX): multilingual, fast, good for most use cases.
- **Whisper / faster-whisper**: heavier, more accurate; set `stt.model_path` in `app.yaml` to use a local file for fully offline operation.

### Voice output (TTS)

- **Edge TTS**: no model download, requires internet, 100+ voices.
- **Kokoro**: local ONNX, ~200ms latency, zh / ja / en.
- **GPT-SoVITS / Qwen3-TTS**: follow the TTS settings page instructions to install dependencies or download weights. Qwen3-TTS requires PyTorch — see below.

### Qwen3-TTS & PyTorch (only if using Qwen3-TTS)

PyTorch is not included in `requirements-ai.txt` to avoid forcing everyone to download GB-sized GPU builds. Install separately:

```bat
# NVIDIA GPU (CUDA — match your driver version)
.venv\Scripts\python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128

# CPU only
.venv\Scripts\python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
```

More combinations at [pytorch.org](https://pytorch.org/). If you see `Torch not compiled with CUDA enabled`, you have the CPU build — either switch to the CUDA build or set the TTS device to `cpu` in TTS settings.

### Screen / vision (VLM)

Configure a multimodal model in the VLM settings to enable screenshot understanding in supported scenarios.

### Ambient tools (weather / trends)

Enable and configure weather and trend tools (RSS / API) in the **Tools** tab. Once enabled, they inject context into prompts and influence proactive speech content.

---

## Document map

| Doc | When to read |
|:---|:---|
| **This doc (GETTING_STARTED)** | UI is running — steps, where to click, what to enable; emotion/energy/affinity/reflection/ASE → section above |
| **SETUP_FIRST_RUN** | Installing Python / Node from scratch, running `init.bat`, Docker, crash fixes |
| **profile_prompts** | Writing or tuning persona JSON by hand; also the source of truth for AI wizard generation |
| **ARCHITECTURE_REFERENCE** | Contributors / architects checking module boundaries and required files |

In the app, **Settings → Onboarding** links to these docs (opens `*.zh.md` or `*.en.md` matching the UI language).
