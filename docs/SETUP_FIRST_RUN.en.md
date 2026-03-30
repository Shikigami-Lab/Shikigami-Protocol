# Environment setup & first run

> Want the short "how many steps / what each Settings tab does"? Read [GETTING_STARTED.en.md](./GETTING_STARTED.en.md).
> **中文：** [SETUP_FIRST_RUN.zh.md](./SETUP_FIRST_RUN.zh.md)

This doc covers: **Python / Node installation, `init.bat` / `init.sh`, Docker deployment, and crash troubleshooting**.

---

## Quick paths

### Pre-built release (Recommended)

Go to [GitHub Releases](https://github.com/Shikigami-Lab/Shikigami-Protocol/releases) and download the installer for your platform:
- **Windows**: `Shikigami-Protocol-Setup-x.y.z.exe` (One-click installer)
- **macOS**: `Shikigami-Protocol-x.y.z.dmg`
- **Linux**: `Shikigami-Protocol-x.y.z.AppImage`

> **Note**: Pre-built releases do not require Python or Node.js. Large components like AI memory vectors and local TTS/STT still require on-demand model downloads via the UI.

### Run from source (Developers/Advanced)

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

Edit `.env` in the project root — add at least one LLM:

```env
GEMINI_API_KEY=your_key_here
# Local Ollama (no key needed):
# OLLAMA_LOCAL_API_KEY=ollama
```

You can also fill these in via **Settings → LLM** in the UI after startup; the UI writes back to `.env` automatically.

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
