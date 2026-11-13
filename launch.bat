@echo off
chcp 65001 >nul
setlocal DisableDelayedExpansion
set PYTHONUTF8=1
title Shikigami Protocol
cd /d "%~dp0" || exit /b 1

rem Version from package.json
set APPVER=
for /f "delims=" %%v in ('node -p "require('./package.json').version" 2^>nul') do set APPVER=%%v
if not defined APPVER set APPVER=0.0.0

echo.
echo   +--------------------------------------+
echo   ^|      Shikigami Protocol  v%APPVER%      ^|
echo   +--------------------------------------+
echo.

rem --- Mode 1: Electron desktop app (requires npm install) ---
if exist "node_modules\.bin\electron.cmd" (
    echo   [Electron] Starting desktop app...
    echo.
    call npm start
    goto electron_done
)

rem --- Mode 2: Browser fallback (use .venv or system python) ---
echo   [Browser] Electron not installed. Starting in browser mode.
echo   Run npm install for full desktop experience.
echo.

rem Prefer .venv Python, then system python
set PYTHON=
if exist ".venv\Scripts\python.exe" (
    set PYTHON=.venv\Scripts\python.exe
) else (
    where python >nul 2>&1
    if not errorlevel 1 (
        set PYTHON=python
    ) else (
        echo   Error: Python not found and no .venv. Run init.bat or pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
)

"%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo   Error: Python 3.10+ required. Run init.bat first.
    exit /b 1
)

echo   Python: %PYTHON%
rem The helper validates config and opens the URL without CMD interpolation.
"%PYTHON%" scripts\launch_browser.py
if errorlevel 1 goto launch_failed
start "" /b "%PYTHON%" scripts\launch_browser.py --open
echo   Press Ctrl+C to stop.
echo.

"%PYTHON%" server.py
exit /b %errorlevel%

:electron_done
exit /b %errorlevel%

:launch_failed
exit /b 1
