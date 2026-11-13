#!/usr/bin/env bash
# Shikigami Protocol — First-time setup (macOS / Linux)
# Equivalent of init.bat for Unix systems.
set -euo pipefail
PYTHONUTF8=1
export PYTHONUTF8

cd "$(dirname "$0")"

echo ""
echo "  +--------------------------------------------+"
echo "  |   Shikigami Protocol - Environment init   |"
echo "  |   (See docs/SETUP_FIRST_RUN.zh.md for CN)    |"
echo "  +--------------------------------------------+"
echo ""

# ── 1. Find Python 3.12 / 3.13 / 3.x ─────────────────────────────────────────
PYTHON=""
for candidate in python3.12 python3.13 python3.11 python3.10 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
            PYTHON=$(command -v "$candidate")
            PYVER=$("$candidate" --version 2>&1)
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "  ERROR: Python 3.10+ not found."
    echo "  Install Python 3.12 (recommended): https://www.python.org/downloads/"
    exit 1
fi

echo "  [1/4] Using Python: $PYVER"
echo "         $PYTHON"
echo ""

# ── 2. Create / validate .venv ────────────────────────────────────────────────
NEED_VENV=1
if [[ -f ".venv/bin/python" ]]; then
    if ".venv/bin/python" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" &>/dev/null; then
        NEED_VENV=0
    fi
fi

if [[ "$NEED_VENV" -eq 1 ]]; then
    if [[ -e ".venv" || -L ".venv" ]]; then
        echo "  ERROR: Existing .venv is invalid or uses Python older than 3.10."
        echo "  Move it aside, then run bash init.sh again. Nothing was deleted."
        exit 1
    fi
    echo "  [2/4] Creating .venv ..."
    "$PYTHON" -m venv .venv
    echo "         Done."
else
    echo "  [2/4] .venv exists and works, skipping."
fi
echo ""

# ── 3. Install Python deps ────────────────────────────────────────────────────
echo "  [3/4] pip install -r requirements.txt ..."
echo ""
.venv/bin/python -m pip install -r requirements.txt
echo "         Done."
echo ""

# ── 4. Optional: Node / npm for Electron desktop ─────────────────────────────
if [[ -f "package.json" ]]; then
    NEED_NPM=0
    [[ ! -d "node_modules" ]] && NEED_NPM=1
    [[ -d "node_modules" && ! -f "node_modules/.bin/electron" ]] && NEED_NPM=1

    if [[ "$NEED_NPM" -eq 1 ]]; then
        if command -v node &>/dev/null; then
            echo "  [4/4] npm install ..."
            if ! npm install; then
                echo "  ERROR: npm install failed. Retry bash init.sh to finish desktop setup."
                exit 1
            fi
            echo ""
        else
            echo "  [4/4] Node.js not found, skipping npm install."
            echo "         Run 'npm install' later if you want the desktop app."
            echo ""
        fi
    else
        echo "  [4/4] node_modules and Electron OK, skipping."
        echo ""
    fi
else
    echo "  [4/4] No package.json, skipping Node."
    echo ""
fi

# ── 5. .env ───────────────────────────────────────────────────────────────────
if [[ ! -f ".env" ]]; then
    if [[ -f ".env.example" ]]; then
        cp .env.example .env
        echo "  Copied .env from .env.example. Edit .env to add API keys."
    fi
else
    echo "  .env already exists, not overwritten."
fi

# ── 6. config/app.yaml ────────────────────────────────────────────────────────
if [[ ! -f "config/app.yaml" ]]; then
    if [[ -f "config/app.yaml.example" ]]; then
        cp config/app.yaml.example config/app.yaml
        echo "  Copied config/app.yaml from app.yaml.example."
    fi
else
    echo "  config/app.yaml already exists, not overwritten."
fi
echo ""

echo "  +--------------------------------------+"
echo "  |   Setup done.                        |"
echo "  |   Start: bash launch.sh              |"
echo "  |   (CN: docs/SETUP_FIRST_RUN.zh.md)      |"
echo "  +--------------------------------------+"
echo ""
