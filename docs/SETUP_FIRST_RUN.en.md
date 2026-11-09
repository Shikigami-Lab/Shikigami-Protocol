# Environment setup & first run

> Want the short "how many steps / what each Settings tab does"? Read [GETTING_STARTED.en.md](./GETTING_STARTED.en.md).
> **中文：** [SETUP_FIRST_RUN.zh.md](./SETUP_FIRST_RUN.zh.md)

This doc covers: **Python / Node installation, `init.bat` / `init.sh`, Docker deployment, and crash troubleshooting**.

---

## Quick paths

**Pre-built desktop installers (.exe / .dmg / .AppImage) are no longer published.** Clone the repo and use **Run from source** below, or **Docker**. [Releases](https://github.com/Shikigami-Lab/Shikigami-Protocol/releases) are version tags with auto-generated notes plus GitHub’s default **source code** archives only; desktop packaging is **under active development**.

### Run from source

#### Windows
```bat
git clone https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
init.bat
launch.bat
```

`init.bat` automatically: detects Python → creates/repairs `.venv` → `pip install -r requirements.txt` (Core dependencies) → (if Node is present) `npm install` → copies `.env.example` to `.env`.

> **AI Components**: Heavy libraries like ChromaDB, ModelScope, and Qwen3-TTS are no longer installed by default. Click the one-click install buttons in **Settings → Onboarding** after launching.

### macOS / Linux

```bash
git clone https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
bash init.sh
bash launch.sh
```

### Docker (server / NAS / no Electron)

```bash
git clone https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
cp .env.example .env        # add your API keys
docker compose up -d
# open http://localhost:7788 in a browser
```

`profiles/`, `models/`, `Voices/`, and `config/` are mounted as volumes — data persists across container restarts. GPU optional: uncomment `deploy.resources` in `docker-compose.yml` (requires nvidia-container-toolkit).

Common commands: `docker compose up -d` / `down` / `logs -f` / `up -d --build`

---

## Prerequisites

### Python 3.10+

**Purpose**: runs `server.py` and all backend logic.

**Windows (pick one)**:
- Official installer: [python.org/downloads](https://www.python.org/downloads/) — check **"Add Python to PATH"** during install
- Microsoft Store: search "Python 3.12"
- winget: `winget install Python.Python.3.12`

**macOS**: `brew install python@3.12` or the official pkg
**Linux**: `sudo apt install python3.12 python3.12-venv` (or distro equivalent)

**Verify** (new terminal): `python --version` or `py -3 --version` → should show 3.10+

### Node.js 18+ (Electron desktop only)

If you only need **browser mode** (`python server.py` + browser at `http://localhost:7788`), skip this.

**Windows**: [nodejs.org](https://nodejs.org/) LTS, or [nvm-windows](https://github.com/coreybutler/nvm-windows) → `nvm install lts` → `nvm use lts`
**macOS / Linux**: `brew install node` or `nvm install --lts`

**Verify**: `node -v`, `npm -v` should both return a version number.

---

## Manual steps (without init scripts)

```bash
# 1. Create virtual environment (first time only)
python -m venv .venv

# 2. Activate and install dependencies
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt

# 3. Desktop: install Node dependencies (first time only)
npm install

# 4. Copy env template (first time only)
cp .env.example .env
# Edit .env — add at least one LLM API key

# 5. Launch
# Desktop (Electron):
npm start
# Backend + browser only:
python server.py
# then open http://localhost:7788
```

---

## Configuring API keys

**Recommended**: after launching the app, go to **Settings → LLM** and type in your API key — it saves automatically.

Alternatively, edit `.env` in the project root before launching (useful for Docker / headless deployments):

```env
GEMINI_API_KEY=your_key_here
# Local Ollama (no key needed):
# OLLAMA_LOCAL_API_KEY=ollama
```

---

## AI tools (Harness, optional)

Lets a character actually perform tasks: read/write files in its workspace, run code, search the web, look up chat history, manage todos and timers, and more. **All tool capabilities are off by default** — if you don't plan to use tools, skip this section entirely; normal chat is unaffected.

> **There is nothing to "start":** Harness is not a background service. Each task is launched by the app itself as a one-shot Docker container that is destroyed when the task ends. After the setup below, daily use is just **keep Docker running + open the app** — no extra startup step exists.

### Step 1: Install and start Docker

Tool code runs inside an isolated Docker container (no network, read-only filesystem), so a container engine is required:

- **Windows**: install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (requires WSL2; the installer guides you). **Keep Docker Desktop running while using tools**; enable "Start Docker Desktop when you sign in" in its settings to avoid starting it manually.
- **macOS**: Docker Desktop for Mac, or a free alternative like [OrbStack](https://orbstack.dev/) / [colima](https://github.com/abiosoft/colima) — anything that provides the `docker` command works.
- **Linux**: `sudo apt install docker.io` (or your distro's equivalent) and start the service; add your user to the `docker` group to skip sudo.

**Verify** (new terminal): `docker version` should show both a Client and a Server section. Client only = the engine isn't running.

### Step 2: Build the Harness image (one time, needs network)

From the repository root:

```bash
docker build -t shikigami-harness:workspace-0.2.1rc2 integrations/deepseek-harness
```

### Step 3: Enable in the app

1. **Settings → Models → Tool execution model**: pick a saved model preset (defaults to following the active chat model).
2. **Settings → Tools**: turn on the master switch and enable capabilities as needed (workspace / code / web / history / memory / todo; code requires workspace). Status showing **ready** means you're set.
3. **Profiles → Tools** (optional): override the global defaults per character — e.g. enable tools for one character only.

For the full capability reference, see [integrations/deepseek-harness/README.md](../integrations/deepseek-harness/README.md).

### Tool errors: what they mean

| Error message | Meaning and fix |
| --- | --- |
| `Docker is not installed or not on PATH` | Docker isn't installed or isn't on PATH → back to Step 1 |
| `Docker is installed but its engine is not running` | Docker is installed but the engine is stopped → open Docker Desktop (or start the docker service), wait until ready, retry |
| `the Harness image is missing` | The image hasn't been built → run the `docker build` from Step 2 |
| `Install the pinned project requirements` | Backend dependencies incomplete → activate `.venv` and run `pip install -r requirements.txt` |
| `AI tools are disabled for this character` | Tools are off for this character → enable them in Settings → Tools or Profiles → Tools |

---

## Troubleshooting

### Instant exit / error code 103

**Symptom**: double-clicking `launch.bat` or Electron closes immediately; terminal shows:
```
[server:err] No Python at 'C:\Users\...\Python312\python.exe'
[server] exited unexpectedly (code=103)
```

**Cause**: the `.venv` folder was copied from another machine. Its `pyvenv.cfg` points to a Python path that doesn't exist here.

**Fix**: run `init.bat` from the project root (it detects and rebuilds `.venv`), or delete the `.venv` folder manually and re-run `pip install -r requirements.txt`.

---

### npm install fails / ECONNRESET

If you're behind a firewall or on a slow connection:

```bat
set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
npm install
```

To set this permanently: `npm config set electron_mirror https://npmmirror.com/mirrors/electron/`

If you get **`EPERM: operation not permitted`**: close any process locking `node_modules` (IDEs, antivirus), restart, delete `node_modules`, then reinstall.

---

### Browser mode only (skip Electron)

You can skip Node.js entirely:

```bash
python server.py
# open http://localhost:7788
```

---

### Blank page at :7788

Wait a few seconds (first launch may load models). If blank after 30 s, check the terminal for error messages.

---

### init.bat says "Python not found"

Confirm Python is in PATH: open a new CMD and run `python --version`. If not found, reinstall Python and check **"Add Python to PATH"** during setup.

---

**Next**: once the environment is ready, read [GETTING_STARTED.en.md](./GETTING_STARTED.en.md) to configure your model and first persona.
