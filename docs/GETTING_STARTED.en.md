# Getting started

> Environment setup (Python, Node, `init.bat`): [SETUP_FIRST_RUN.en.md](./SETUP_FIRST_RUN.en.md).
> Persona JSON fields: [profile_prompts.en.md](./profile_prompts.en.md).
> **中文：** [GETTING_STARTED.zh.md](./GETTING_STARTED.zh.md)

**Start the app from source (desktop/browser) or Docker first:** [SETUP_FIRST_RUN.en.md](./SETUP_FIRST_RUN.en.md). Then use the two steps below.

---

## Minimum steps to start chatting

| Step | What to do | Where |
|:---:|:---|:---|
| **1** | Configure at least **one chat model** (cloud API key or local base URL) | Settings → **Models** |
| **2** | Open a persona card (built-in or made with the AI Wizard), check **Load to chat**, then select that character in the left sidebar | Settings → **Profiles**, then the chat sidebar |

Steps **1 + 2** are all you need to start chatting in the main window. Selecting a card in Settings only opens it for editing; **Load to chat** adds it to the chat list. For Docker, configure presets and keys in host files as described in [first-run setup](./SETUP_FIRST_RUN.en.md#configuring-api-keys), because UI preset saves also write the read-only-mounted `.env`.

> Environment not set up yet? See [SETUP_FIRST_RUN.en.md](./SETUP_FIRST_RUN.en.md).
> Can't reach Gemini / OpenAI from your network? See the **Proxy** section below.

---

## AI tools & workspace (optional)

Basic chat needs no Harness. For tasks, first complete [Harness setup](./SETUP_FIRST_RUN.en.md#ai-tools-harness-optional): a Linux-container Docker engine and a separately built image are required. AI tools, capabilities and reflection-initiated tasks are **off by default**.

1. **Settings → Models → Tool execution model**: select a saved preset, or follow the active chat model. Test its connection separately.
2. **Settings → Tools**: enable AI tools and the required capabilities. Code needs workspace access. **Profiles → Tools** provides on/off/inherit overrides and separate timer/todo permissions. Local runtime readiness does not prove a successful model call.
3. Try a small task, such as finding an original message and writing a cited note (history + workspace access). The chat model decides whether to delegate and writes the final reply; the tool model executes the task. Open the chat-header workspace button for **Files / Plans / Tasks**. Stop preserves completed work; Continue starts a fresh run from saved records.

---

## Proxy (restricted networks)

> Most users can skip this section. Only needed if your network cannot reach Gemini / OpenAI or other cloud APIs directly.

The backend is a **Python process** — "the browser can use a VPN" does not mean Python can too.

**Recommended**:

1. Open **Settings → System**.
2. Enable **Use proxy**. If HTTP and HTTPS requests share one proxy, enter the same address in both fields, for example `http://127.0.0.1:<port>`. Use the HTTP or mixed port shown in your proxy app.
3. Click **Save proxy settings**. If an existing service connection still uses the old address, restart the app to reconnect.

To open settings through a reverse-proxy hostname, add it to `SHIKIGAMI_SETTINGS_TRUSTED_HOSTS` on the server. Separate multiple hostnames with commas. The reverse proxy must preserve the browser-facing `Host` and forward the HTTPS scheme through a trusted proxy connection.

If you're unsure of the port: check the proxy app's local HTTP port setting, or run `netstat -ano | findstr LISTENING` in CMD and look for common proxy ports (`7890`, `10808`, etc.) on `127.0.0.1`.

You can also edit **`.env`** directly to set `HTTP_PROXY` / `HTTPS_PROXY` — same effect as the Settings page.

---

## Settings tabs overview

| Tab | Purpose |
|:---|:---|
| **First steps** | **Step-by-step guide**: checklist, network, memory/TTS/STT; companion behavior points to settings — details in the “Emotion, energy…” section below. |
| **Profiles** | Persona card list, edit, **AI Wizard** generation, sidebar order, per-persona memory / engine overrides; **Prompt Editing** sub-tab includes Persona Evolution |
| **Models** | LLM preset management, API keys, local OpenAI-compatible URLs, models for analysis, reflection, tools, vision, and embeddings |
| **Memory settings** | Global and per-persona extraction, retrieval, summaries, review settings and task prompts |
| **Voice** | Voice engines: Edge / GPT-SoVITS / Kokoro / Qwen3-TTS |
| **Behavior** | My profile, reflection, proactive messages, topic sources, screenshot behavior, and character state |
| **Tools** | AI tool defaults, capability permissions, runtime status and task limits; existing todo, timer, weather and search settings |
| **System** | Theme, proxy, HF mirror, offline toggle |

Browse facts, vectors, summaries and chat history from the **sidebar memory entry**, separately from Memory settings. The **chat-header workspace button** opens files, plans and task records.

Persona cards can also configure emotion/energy/affinity prompts, memory strategy, and TTS reference audio — see [profile_prompts.en.md](./profile_prompts.en.md) for field reference.

---

## Emotion, energy, affinity, reflection & ASE

These features go beyond plain chat: the persona has **mood, stamina, long-term rapport**, can **reflect in the background**, and may **message you first** after silence. Defaults work without editing prompts; tune below if you want clingier, calmer, or more “tool-like” behavior.

### Emotion engine

- **What it does**: Classifies layered emotions from dialogue, shaping reply tone (and TTS instruct if used).
- **Where**: **Settings → Profiles → Behavior** toggles the engine; per-emotion **rules** live in the persona emotion prompt areas (`emotion_prompts`, `emotion_zh_descriptions`). See [profile_prompts.en.md](./profile_prompts.en.md) → `emotion_config`.

### Energy system

- **What it does**: Chat drains energy; it recovers over offline/wall-clock time. Bands map to “more/less talkative” via `energy_prompts`.
- **Where**: Defaults in `config/app.yaml` → `engines.energy`; per-persona energy behavior in **Profiles → Behavior**; band texts in **energy prompt** overrides (write **how to speak**, not scene narration).

### Affinity engine

- **What it does**: Adjusts an affinity score from recent dialogue (tiers from stranger to bonded). `affinity_prompts` set distance, tone, and boundaries per tier.
- **Where**: **Profiles → Behavior**; auxiliary LLM in **Models** settings; tier text in **affinity prompts** on the persona. See `profile_prompts.en.md`.

### Reflection

- **What it does**: Background LLM runs produce short inner state (e.g. topic hints) for chat and ASE. **Uses tokens on a schedule.**
- **Where**: **Settings → Behavior → Reflection** for global controls, **Models → reflection model** for the preset, and **Profiles → Behavior** for per-persona overrides. Prefer a **cheap or local** model. Save segments as the UI indicates.

### Proactive speech (ASE)

- Under silence, urgency, cooldown, and daily caps, the persona may **send a proactive line**. It can draw on local topics, weather, and optional VLM context.
- **Where**: **Settings → Behavior → Proactive messages** for the ASE toggle, mode, topic sources and screenshot behavior; timing thresholds largely in `config/app.yaml` → `ase.modes`.

**Tips**: Stabilize base persona before enabling ASE. If too chatty, raise urgency threshold or use **low** mode first.

### Persona Evolution

- **What it does**: Every N conversation turns (same counter as emotion and affinity engines), the AI quietly rewrites `base_prompt` and `style_constraint` — nudged by accumulated facts and affinity state. The persona grows through shared experience rather than being frozen at the first draft forever.
- **Core anchor**: You (or the AI's "Re-extract" button) write a few semicolon-separated immutable traits, e.g. `hides warmth behind distance; never admits she cares first; can't be pushed around`. These are passed as a hard constraint on every rewrite, guarding against the RLHF drift that turns every character into a polite, soulless chatbot.
- **Where**: **Settings → Profiles → Prompt Editing**, in the **Persona Evolution** collapsible below Style Constraint:
  - **Enable toggle** (off by default) and **trigger interval** (every N conversation turns, default 200 — uses the same counter as the emotion/affinity engines).
  - **Re-extract**: let the AI distill a core anchor from the current persona text — edit it afterwards as you like.
  - **Original persona (read-only)**: your hand-written original, never touched by evolution, always visible for comparison.
  - **Current evolved version**: the live persona; editable by hand and auto-saved on change.
  - **Evolution log**: per-run change summary and rationale; click **Roll back** to restore any prior version.
- **Tips**: Let the persona accumulate a few dozen turns of memory before enabling. If you've crafted a very precise persona and don't want autonomous rewrites, leave it off — the anchor and changelog are still useful as read-only context.

---

## Optional enhancements

### Memory & vector retrieval

- Vector retrieval is **off by default**: install optional `chromadb` from **Settings → First steps**, then enable vector retrieval in **Memory settings**. Keyword search remains available without it.
- **Cloud embedding (Gemini etc.)**: configure the provider, model and key under **Settings → Models → Embedding model**, then enable vectors in Memory settings.
- **Local vector retrieval** (no cloud key): download a BGE-small or similar model to `models/` via the First steps tab (HF mirror / ModelScope), then select local embeddings and the model path under **Models → Embedding model**.
- If `HF_HUB_OFFLINE=1` is set, online downloads will fail — disable offline mode or provide model files manually.

### Voice input (STT)

In the First steps tab, select an STT engine and download the model:
- **SenseVoice** (ONNX): multilingual, fast, good for most use cases.
- **Whisper / faster-whisper**: heavier, more accurate; set `stt.model_path` in `app.yaml` to use a local file for fully offline operation.

### Voice output (TTS)

- **Edge TTS**: no model download, requires internet, 100+ voices.
- **Kokoro**: local ONNX, ~200ms latency, zh / ja / en.
- **GPT-SoVITS / Qwen3-TTS**: follow **Settings → Voice** to install dependencies or download weights. Qwen3-TTS requires PyTorch — see below.

### Qwen3-TTS & PyTorch (only if using Qwen3-TTS)

PyTorch is not included in `requirements-ai.txt` to avoid forcing everyone to download GB-sized GPU builds. Install separately:

```powershell
# NVIDIA GPU (CUDA — match your driver version)
.\.venv\Scripts\python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128

# CPU only
.\.venv\Scripts\python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
```

On macOS/Linux use `.venv/bin/python` in place of `.\.venv\Scripts\python`; choose a PyTorch build for your OS and hardware.

More combinations at [pytorch.org](https://pytorch.org/). If you see `Torch not compiled with CUDA enabled`, you have the CPU build — either switch to the CUDA build or set the TTS device to `cpu` in TTS settings.

### Screen / vision (VLM)

Configure a multimodal model under **Settings → Models → Vision model**, then adjust screenshots under **Behavior → Proactive messages** for supported scenarios.

### Weather awareness

Enable and configure weather in the **Tools** tab to give the character local weather context.

---

## Document map

| Doc | When to read |
|:---|:---|
| **This doc (GETTING_STARTED)** | UI is running — steps, where to click, what to enable; emotion/energy/affinity/reflection/ASE → section above |
| **SETUP_FIRST_RUN** | Installing Python / Node from scratch, running `init.bat`, Docker, crash fixes |
| **profile_prompts** | Writing or tuning persona JSON by hand; also the source of truth for AI wizard generation |
| **ARCHITECTURE_REFERENCE** | Contributors / architects checking module boundaries and required files |

In the app, **Settings → First steps** links to these docs (opens `*.zh.md` or `*.en.md` matching the UI language).
