# Getting started

> Environment setup (Python, Node, `init.bat`): [SETUP_FIRST_RUN.en.md](./SETUP_FIRST_RUN.en.md).
> Persona JSON fields: [profile_prompts.en.md](./profile_prompts.en.md).
> **中文：** [GETTING_STARTED.zh.md](./GETTING_STARTED.zh.md)

**This guide assumes your environment is already set up and the backend is running (browser can open `http://localhost:7788`).**

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
| **Onboarding** | **Step-by-step Guide (Recommended)**: network setup, AI memory deps & models, TTS / STT deps & models. |
| **Personas** | Persona card list, edit, **AI Wizard** generation, sidebar order, per-persona memory / engine overrides |
| **LLM** | LLM preset management, API keys, local OpenAI-compatible URLs, auxiliary models (emotion / affinity) |
| **Memory** | Long-term facts, vector memory toggle, embedding model (cloud Gemini or local BGE), diary and forgetting preview |
| **TTS** | Voice engines: Edge / GPT-SoVITS / Kokoro / Qwen3-TTS |
| **自省 / ASE** | Character inner monologue, proactive speech configuration |
| **VLM** | Screen / visual understanding: model and screenshot strategy |
| **Tools** | Todos, timers, weather, trends, web search — plugin toggles and config |
| **System** | Theme, proxy, HF mirror, offline toggle |

Persona cards can also configure emotion/energy/affinity prompts, memory strategy, and TTS reference audio — see [profile_prompts.en.md](./profile_prompts.en.md) for field reference.

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
| **This doc (GETTING_STARTED)** | UI is running — want to know "how many steps, where to click, what to enable" |
| **SETUP_FIRST_RUN** | Installing Python / Node from scratch, running `init.bat`, Docker, crash fixes |
| **profile_prompts** | Writing or tuning persona JSON by hand; also the source of truth for AI wizard generation |
| **ARCHITECTURE_REFERENCE** | Contributors / architects checking module boundaries and required files |

In the app, **Settings → Onboarding** links to these docs (opens `*.zh.md` or `*.en.md` matching the UI language).
