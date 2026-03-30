#!/usr/bin/env bash
# Shikigami Protocol — Start script (macOS / Linux)
# Equivalent of launch.bat for Unix systems.
# Mode 1: Electron desktop (if npm install was run)
# Mode 2: Browser fallback (python server.py, then open browser)
set -euo pipefail

cd "$(dirname "$0")"

# Version from package.json (requires node)
APPVER="0.0.0"
if command -v node &>/dev/null && [[ -f "package.json" ]]; then
    APPVER=$(node -p "require('./package.json').version" 2>/dev/null || echo "0.0.0")
fi

echo ""
echo "  +--------------------------------------+"
echo "  |   Shikigami Protocol  v${APPVER}   |"
echo "  +--------------------------------------+"
echo ""

# ── Mode 1: Electron desktop ──────────────────────────────────────────────────
ELECTRON_BIN=""
[[ -f "node_modules/.bin/electron" ]] && ELECTRON_BIN="node_modules/.bin/electron"

if [[ -n "$ELECTRON_BIN" ]]; then
    echo "  [Electron] Starting desktop app..."
    echo ""
    npm start
    exit $?
fi

# ── Mode 2: Browser fallback ──────────────────────────────────────────────────
echo "  [Browser] Electron not installed. Starting in browser mode."
echo "  Run 'npm install' for full desktop experience."
echo ""

# Prefer .venv Python, then system python3 / python
PYTHON=""
if [[ -f ".venv/bin/python" ]]; then
    PYTHON=".venv/bin/python"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
elif command -v python &>/dev/null; then
    PYTHON="python"
else
    echo "  Error: Python not found and no .venv. Run bash init.sh first."
    exit 1
fi

SRV_PORT=7788

# Open browser after server starts (~3s)
(
    sleep 3
    URL="http://127.0.0.1:${SRV_PORT}"
    if command -v open &>/dev/null; then          # macOS
        open "$URL"
    elif command -v xdg-open &>/dev/null; then    # Linux
        xdg-open "$URL"
    fi
) &

echo "  Python: $PYTHON"
echo "  Server starting at http://127.0.0.1:${SRV_PORT}"
echo "  Press Ctrl+C to stop."
echo ""

exec "$PYTHON" server.py
