@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title Shikigami Protocol
cd /d "%~dp0"

rem Version from package.json
for /f "delims=" %%v in ('node -p "require('./package.json').version" 2^>nul') do set APPVER=%%v
if not defined APPVER set APPVER=0.0.0

echo.
echo   +--------------------------------------+
echo   ^|      Shikigami Protocol  v!APPVER!      ^|
echo   +--------------------------------------+
echo.

rem --- Mode 1: Electron desktop app (requires npm install) ---
if exist "node_modules\.bin\electron.cmd" (
    echo   [Electron] Starting desktop app...
    echo.
    call npm start
    exit /b %errorlevel%
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
    if %errorlevel% equ 0 (
        set PYTHON=python
    ) else (
        echo   Error: Python not found and no .venv. Run init.bat or pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
)

rem Port must match config/app.yaml (default 7788)
set SRV_PORT=7788
rem Open browser after 3s
start /min "" cmd /c "timeout /t 3 /nobreak >nul 2>&1 && start http://127.0.0.1:%SRV_PORT%"

echo   Python: %PYTHON%
echo   Server starting at http://127.0.0.1:%SRV_PORT%
echo   Press Ctrl+C to stop.
echo.

%PYTHON% server.py
