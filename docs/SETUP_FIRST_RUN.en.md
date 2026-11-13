# Environment setup & first run

> Want the short "how many steps / what each Settings tab does"? Read [GETTING_STARTED.en.md](./GETTING_STARTED.en.md).
> **中文：** [SETUP_FIRST_RUN.zh.md](./SETUP_FIRST_RUN.zh.md)

This doc covers: **Python / Node installation, `init.bat` / `init.sh`, Docker deployment, and crash troubleshooting**.

---

## Quick paths

**v0.12.0 Public Beta is being prepared on `dev`, not yet released.** The commands below select `dev`; default `main` and older [Release archives](https://github.com/Shikigami-Lab/Shikigami-Protocol/releases) do not contain these updates. Use source for Electron or browser mode, or Docker for the main Web UI. No v0.12.0 pre-built desktop installer is available. These are setup requirements, not a guarantee that every platform, CPU architecture or optional component has passed acceptance.

### Run from source

#### Windows (PowerShell)
```powershell
git clone --branch dev https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
.\init.bat
.\launch.bat
```

`init.bat` detects Python, prepares `.venv`, installs core requirements and (if Node is present) Node dependencies, and copies missing configuration templates. If it reports an invalid or outdated environment, preserve or rename the old `.venv` before rerunning setup.

> **AI Components**: Heavy libraries like ChromaDB, ModelScope, and Qwen3-TTS are no longer installed by default. Click the one-click install buttons in **Settings → First steps** after launching.

### macOS / Linux

```bash
git clone --branch dev https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
bash init.sh
bash launch.sh
```

### Docker (server / NAS / no Electron)

```bash
git clone --branch dev https://github.com/Shikigami-Lab/Shikigami-Protocol.git
cd Shikigami-Protocol
cp .env.example .env        # add your API keys
docker compose up -d
# open http://localhost:7788 in a browser
```

The block above uses macOS/Linux shell syntax. In PowerShell use `Copy-Item .env.example .env`, or `copy .env.example .env` in CMD (new installs only).

`data/`, `profiles/`, `models/`, `Voices/`, and `config/` are mounted as volumes; `.env` is read-only, so edit keys on the host. Before recreating an older container, migrate any container-only `/app/data` as described under [Upgrading](#upgrading-from-v0110). GPU optional: uncomment `deploy.resources` in `docker-compose.yml` (requires nvidia-container-toolkit).

This starts the **main application and Web UI**, separately from **Harness task containers**. The supplied main image has no Docker CLI and Compose does not mount a Docker socket; `docker compose up` alone does not provide Harness access. For Harness, run the application from source on the host and follow the section below.

Common commands: `docker compose up -d` / `down` / `logs -f` / `up -d --build`

---

## Prerequisites

### Python 3.12 (recommended)

**Purpose**: runs `server.py` and all backend logic. **Use Python 3.12**, the baseline for source-install tests. The project declares 3.10+, but heavy optional AI packages are not guaranteed to work with the newest Python 3.14/3.15. Create the virtual environment using 3.12.

**Windows (pick one)**:
- Official installer: [python.org/downloads](https://www.python.org/downloads/) — check **"Add Python to PATH"** during install
- Microsoft Store: search "Python 3.12"
- winget: `winget install Python.Python.3.12`

**macOS**: `brew install python@3.12` or the official pkg
**Linux**: `sudo apt install python3.12 python3.12-venv` (or distro equivalent)

**Verify** (new terminal): Windows `py -3.12 --version`; macOS/Linux `python3.12 --version` → 3.12.x.

Check which Python the init script selects. If it chooses a different version, use the explicit 3.12 commands under **Manual steps** below.

### Node.js 18+ (Electron desktop only)

If you only need **browser mode** (`python server.py` + browser at `http://localhost:7788`), skip this.

**Windows**: [nodejs.org](https://nodejs.org/) LTS, or [nvm-windows](https://github.com/coreybutler/nvm-windows) → `nvm install lts` → `nvm use lts`
**macOS / Linux**: `brew install node` or `nvm install --lts`

**Verify**: `node -v`, `npm -v` should both return a version number.

---

## Manual steps (without init scripts)

**Windows (CMD)**

```bat
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
copy config\app.yaml.example config\app.yaml
.venv\Scripts\python server.py
```

**macOS / Linux**

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
cp config/app.yaml.example config/app.yaml
.venv/bin/python server.py
```

Copy templates only on a **new install**; preserve existing configuration during upgrades. Open `http://localhost:7788` in a browser. For Electron, install Node, run `npm install`, then use the platform launch script instead of starting `server.py` separately. In PowerShell prefix the Windows `.venv` commands with `.\` and replace `copy` with `Copy-Item`.

---

## Configuring API keys

**Recommended**: open **Settings → Models**, choose or create a preset, fill in its provider, model ID, base URL and API key, save and test the connection, then select it for chat. Local models need a running endpoint and its actual installed model ID. In Docker, edit keys in the read-only-mounted `.env` on the host; a failed UI save does not mean the key was persisted.

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

Harness requires a **Linux-container Docker engine** accessible to the host application. Code runs without direct network access, with a read-only container root and a writable character workspace when permitted. Model and controlled web requests use the host bridge. Requirements by platform:

- **Windows**: install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (requires WSL2; the installer guides you). **Keep Docker Desktop running while using tools**; enable "Start Docker Desktop when you sign in" in its settings to avoid starting it manually.
- **macOS**: use a Linux-container engine such as [Docker Desktop](https://www.docker.com/products/docker-desktop/). The application must reach the engine and its bind mounts. Intel and Apple Silicon each need a successful native image build and runtime check; a `docker` command alone is insufficient.
- **Linux**: `sudo apt install docker.io` (or your distro's equivalent) and start the service; add your user to the `docker` group to skip sudo.

**Verify** (new terminal): `docker version` must show Client and Server; `docker info --format "{{.OSType}}"` must return `linux`. The app process must have the same engine access. Build/runtime checks are still needed for the host CPU architecture.

### Step 2: Build the Harness image (one time, needs network)

From the repository root:

```bash
docker build -t shikigami-harness:workspace-0.2.1rc2 integrations/deepseek-harness
```

### Step 3: Enable in the app

1. **Settings → Models → Tool execution model**: pick a saved model preset (defaults to following the active chat model).
2. **Settings → Tools**: turn on the AI tools default and enable capabilities as needed (workspace / code / web / history / memory / todo; code requires workspace). **ready** checks local prerequisites and effective permissions, not a live model call: test that separately under Models, then try a small task.
3. **Settings → Profiles → Tools** (optional): permissions can be on, off or inherited; a character can opt in while global defaults stay off. Timer/todo permissions remain separate. Check ordinary tool use before enabling reflection-initiated tasks.

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

## Upgrading from v0.11.0

1. Stop the application and tool workers. Back up matching old source/dependency versions plus **`.env`, all of `config/`, `profiles/` and `data/`**. Include character workspaces (`profiles/<id>/workspace/`), chat/memory databases, task records and workspace versions in `data/agent/`. Preserve SQLite `-wal` / `-shm` files if present; a workspace snapshot is not a full backup. Preserve `models/` and `Voices/` if you installed local assets.
2. **Older Docker deployments**: if `/app/data` was not bind-mounted, stop the old container and copy that directory out before recreating or removing it. Back it up and place the preserved contents in the host `data/` directory used by the new Compose mount. An empty mount hides old container data; it does not migrate it.
3. Update source while retaining configuration and data; do not overwrite them with templates or replace the installation with an empty archive. Reinstall requirements in the app environment: Windows `.\.venv\Scripts\python -m pip install -r requirements.txt`; macOS/Linux `.venv/bin/python -m pip install -r requirements.txt`. Refresh Electron dependencies with `npm install` if used. Rebuild the Harness image if used; for main-app Docker run `docker compose up -d --build` after data migration.
4. Start with AI tools disabled. Check saved model configuration, history, memory and workspace files, then enable needed permissions. Schema updates can add database columns: rollback requires stopping the app and restoring matching old source, dependencies/image and backed-up data together. Preserve newer work separately first.

---

## Troubleshooting

### Instant exit / error code 103

**Symptom**: double-clicking `launch.bat` or Electron closes immediately; terminal shows:
```
[server:err] No Python at 'C:\Users\...\Python312\python.exe'
[server] exited unexpectedly (code=103)
```

**Cause**: the `.venv` folder was copied from another machine. Its `pyvenv.cfg` points to a Python path that doesn't exist here.

**Fix**: close the app, preserve or rename the old `.venv` (e.g. `.venv-old`), then run `init.bat` from the project root to create a fresh environment. Keep the old environment and your configuration/data backup for rollback.

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
