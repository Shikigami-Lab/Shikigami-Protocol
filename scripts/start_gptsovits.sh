#!/usr/bin/env bash
# GPT-SoVITS API Quick Launcher (Linux / macOS)
# Shikigami Protocol

set -euo pipefail

echo "============================================================"
echo " GPT-SoVITS API Quick Launcher"
echo " Shikigami Protocol"
echo "============================================================"
echo

# 1) Resolve GPT-SoVITS directory (set via UI as GPTSOVITS_DIR)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${GPTSOVITS_DIR:-}" ]]; then
  TOOLS_DIR="$GPTSOVITS_DIR"
else
  TOOLS_DIR="$SCRIPT_DIR/../tools/GPT-SoVITS"
fi

if [[ ! -f "$TOOLS_DIR/api_v2.py" ]]; then
  echo "[ERROR] Could not find api_v2.py in: $TOOLS_DIR"
  echo "        Please configure the GPT-SoVITS directory in Shikigami settings."
  echo
  exit 1
fi

echo "[INFO] Using GPT-SoVITS directory: $TOOLS_DIR"
echo

# 2) Choose Python interpreter
PYTHON_CMD="python3"
if [[ -x "$TOOLS_DIR/runtime/python" ]]; then
  PYTHON_CMD="$TOOLS_DIR/runtime/python"
fi

echo "[INFO] Using Python: $PYTHON_CMD"
echo

# 3) Ensure required Python packages
if ! "$PYTHON_CMD" -c "import soundfile" >/dev/null 2>&1; then
  echo "[INFO] Python package 'soundfile' is missing."
  echo "[INFO] Installing: soundfile"
  "$PYTHON_CMD" -m pip install soundfile
fi

echo
echo "[INFO] Launching GPT-SoVITS API v2 ..."
echo "       Dir : $TOOLS_DIR"
echo "       Note: keep this terminal open while using gpt_sovits TTS."
echo

cd "$TOOLS_DIR"
"$PYTHON_CMD" api_v2.py

#!/usr/bin/env bash
# GPT-SoVITS API 服务快速启动脚本 (Linux / macOS)
# Shikigami Protocol — GPT-SoVITS Quick Launcher

set -euo pipefail

GPTSVTS_PORT=9880
GPTSVTS_HOST="127.0.0.1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_DIR="$SCRIPT_DIR/../tools/GPT-SoVITS"

echo "============================================================"
echo " GPT-SoVITS API 服务快速启动脚本"
echo " Shikigami Protocol — GPT-SoVITS Quick Launcher"
echo "============================================================"
echo ""

# ── 1. 检查端口是否已在监听 ──────────────────────────────────────────────────
if command -v nc &>/dev/null; then
    if nc -z "$GPTSVTS_HOST" "$GPTSVTS_PORT" 2>/dev/null; then
        echo "[OK] GPT-SoVITS 已在端口 $GPTSVTS_PORT 运行，无需重新启动。"
        echo "     如需重启，请先 kill 现有进程。"
        exit 0
    fi
elif command -v lsof &>/dev/null; then
    if lsof -i ":$GPTSVTS_PORT" 2>/dev/null | grep -q LISTEN; then
        echo "[OK] GPT-SoVITS 已在端口 $GPTSVTS_PORT 运行，无需重新启动。"
        exit 0
    fi
fi

# ── 2. 寻找 GPT-SoVITS ──────────────────────────────────────────────────────
echo "[INFO] 正在寻找 GPT-SoVITS..."

API_SCRIPT=""
FOUND_DIR=""

# 搜索路径列表
SEARCH_PATHS=(
    "$TOOLS_DIR/api_v2.py"
    "$HOME/GPT-SoVITS/api_v2.py"
    "$HOME/Desktop/GPT-SoVITS/api_v2.py"
    "$HOME/Downloads/GPT-SoVITS/api_v2.py"
    "/opt/GPT-SoVITS/api_v2.py"
)

for path in "${SEARCH_PATHS[@]}"; do
    if [[ -f "$path" ]]; then
        API_SCRIPT="$path"
        FOUND_DIR="$(dirname "$path")"
        echo "[OK] 找到: $path"
        break
    fi
done

# 旧版 api.py fallback
if [[ -z "$API_SCRIPT" ]]; then
    FALLBACK_PATHS=(
        "$TOOLS_DIR/api.py"
        "$HOME/GPT-SoVITS/api.py"
        "$HOME/Desktop/GPT-SoVITS/api.py"
    )
    for path in "${FALLBACK_PATHS[@]}"; do
        if [[ -f "$path" ]]; then
            API_SCRIPT="$path"
            FOUND_DIR="$(dirname "$path")"
            echo "[OK] 找到 (旧版): $path"
            break
        fi
    done
fi

# ── 3. 未找到 — 安装指引 ──────────────────────────────────────────────────────
if [[ -z "$API_SCRIPT" ]]; then
    echo ""
    echo "[未找到 GPT-SoVITS]"
    echo ""
    echo " 请按以下步骤安装 GPT-SoVITS，然后重新运行此脚本："
    echo ""
    echo " 方式 A（推荐）：从 GitHub 下载整合包"
    echo "   1. 访问: https://github.com/RVC-Boss/GPT-SoVITS/releases"
    echo "   2. 下载最新 release 的 Linux/macOS 包（约 2-4 GB）"
    echo "   3. 解压后运行此脚本"
    echo ""
    echo " 方式 B：将目录放到项目内（推荐）："
    echo "   1. 解压后将整个文件夹移动到："
    echo "      $SCRIPT_DIR/../tools/GPT-SoVITS/"
    echo "   2. 重新运行此脚本"
    echo ""
    echo " 安装完成后，在 Shikigami Protocol 设置中将 TTS 切换为 gpt_sovits 即可。"
    exit 1
fi

# ── 4. 启动 ──────────────────────────────────────────────────────────────────
SCRIPT_NAME="$(basename "$API_SCRIPT")"
echo ""
echo "[启动] GPT-SoVITS API 服务..."
echo "      地址：http://$GPTSVTS_HOST:$GPTSVTS_PORT"
echo "      目录：$FOUND_DIR"
echo ""
echo " 启动后请勿关闭此终端。在 Shikigami Protocol 设置中将 TTS 切换为 gpt_sovits。"
echo " 按 Ctrl+C 可停止服务。"
echo ""

cd "$FOUND_DIR"
python3 "$SCRIPT_NAME" -a "$GPTSVTS_HOST" -p "$GPTSVTS_PORT"
