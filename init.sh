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
        ver=$("$candidate" -c "import sys; print(sys.version_info[:2])" 2>/dev/null || true)
        if [[ -n "$ver" ]]; then
            PYTHON=$(command -v "$candidate")
            PYVER=$("$candidate" --version 2>&1)
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "  ERROR: Python not found."
    echo "  Install Python 3.12+: https://www.python.org/downloads/"
    exit 1
fi

echo "  [1/4] Using Python: $PYVER"
echo "         $PYTHON"
echo ""

# ── 2. Create / validate .venv ────────────────────────────────────────────────
NEED_VENV=1
if [[ -f ".venv/bin/python" ]]; then
    if ".venv/bin/python" --version &>/dev/null; then
        NEED_VENV=0
    fi
fi

if [[ "$NEED_VENV" -eq 1 ]]; then
    if [[ -d ".venv" ]]; then
        echo "  [2/4] Existing .venv invalid or from another system, recreating..."
        rm -rf .venv
    else
        echo "  [2/4] Creating .venv ..."
    fi
    "$PYTHON" -m venv .venv
    echo "         Done."
else
    echo "  [2/4] .venv exists and works, skipping."
fi
echo ""

# ── 3. Install Python deps ────────────────────────────────────────────────────
echo "  [3/4] pip install -r requirements.txt ..."
echo ""
.venv/bin/pip install -r requirements.txt
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
            npm install
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
echo ""

echo "  +--------------------------------------+"
echo "  |   Setup done.                        |"
echo "  |   Start: bash launch.sh              |"
echo "  |   (CN: docs/SETUP_FIRST_RUN.zh.md)      |"
echo "  +--------------------------------------+"
echo ""
