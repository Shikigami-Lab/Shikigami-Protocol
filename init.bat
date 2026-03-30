@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
title Shikigami Protocol - First-time setup
cd /d "%~dp0"

echo.
echo   +--------------------------------------------+
echo   ^|   Shikigami Protocol - Environment init  ^|
echo   ^|   (See docs/SETUP_FIRST_RUN.zh.md for CN)   ^|
echo   +--------------------------------------------+
echo.

rem --- 1. Find Python (py, python, or common install paths) ---
rem     Prefer 3.12/3.13: ML packages (tokenizers, torch) lack 3.14 wheels
set PYTHON=
where py >nul 2>&1
if %errorlevel% equ 0 (
    for /f "delims=" %%e in ('py -3.12 -c "import sys; print(sys.executable)" 2^>nul') do set PYTHON=%%e
    if not defined PYTHON (
        for /f "delims=" %%e in ('py -3.13 -c "import sys; print(sys.executable)" 2^>nul') do set PYTHON=%%e
    )
    if not defined PYTHON (
        for /f "delims=" %%e in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set PYTHON=%%e
    )
)
if not defined PYTHON (
    where python >nul 2>&1
    if %errorlevel% equ 0 (
        for /f "delims=" %%e in ('python -c "import sys; print(sys.executable)" 2^>nul') do set PYTHON=%%e
    )
)
rem Fallback: common Windows paths when Python not on PATH
if not defined PYTHON (
    for %%v in (312 311 310 313 39) do (
        if not defined PYTHON if exist "%LocalAppData%\Programs\Python\Python%%v\python.exe" set PYTHON=%LocalAppData%\Programs\Python\Python%%v\python.exe
        if not defined PYTHON if exist "%ProgramFiles%\Python%%v\python.exe" set PYTHON=%ProgramFiles%\Python%%v\python.exe
    )
)
if not defined PYTHON (
    echo   ERROR: Python not found.
    echo.
    echo   Possible reasons:
    echo   - Python not installed. Download: https://www.python.org/downloads/
    echo   - Python installed but "Add to PATH" was not checked. Reinstall and check it.
    echo   - PATH not updated in this window. Close and open a NEW CMD, then run init.bat again.
    echo.
    pause
    exit /b 1
)

for /f "delims=" %%v in ('"%PYTHON%" --version 2^>^&1') do set PYVER=%%v
echo   [1/4] Using Python: %PYVER%
echo          %PYTHON%
echo.

rem --- 2. Create or recreate .venv ---
set NEED_VENV=1
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" --version >nul 2>&1
    if not errorlevel 1 set NEED_VENV=0
)
if %NEED_VENV% equ 1 (
    if exist ".venv" (
        echo   [2/4] Existing .venv invalid or from another PC, recreating...
        rd /s /q .venv
    ) else (
        echo   [2/4] Creating .venv ...
    )
    "%PYTHON%" -m venv .venv
    if errorlevel 1 (
        echo   ERROR: Failed to create venv.
        pause
        exit /b 1
    )
    echo         Done.
) else (
    echo   [2/4] .venv exists and works, skipping.
)
echo.

rem --- 3. Install Python deps ---
echo   [3/4] pip install -r requirements.txt ...
echo.
call .venv\Scripts\pip install -r "requirements.txt"
if errorlevel 1 (
    echo   ERROR: pip install failed. Check network or retry later.
    pause
    exit /b 1
)
echo         Done.
echo.

rem --- 4. Optional: Node (for Electron desktop) ---
rem    Run npm install if node_modules missing OR Electron not present (e.g. previous install failed)
set _NEED_NPM=0
if exist "package.json" (
    if not exist "node_modules" set _NEED_NPM=1
    if exist "node_modules" if not exist "node_modules\.bin\electron.cmd" set _NEED_NPM=1
)
if %_NEED_NPM% equ 1 (
    where node >nul 2>&1
    if not errorlevel 1 (
        if not defined ELECTRON_MIRROR set "ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/"
        echo   [4/4] npm install ...
        call npm install
        echo.
    ) else (
        echo   [4/4] Node.js not found, skipping npm install.
        echo         Run npm install later if you want the desktop app.
        echo.
    )
) else (
    if exist "package.json" (
        echo   [4/4] node_modules and Electron OK, skipping.
        echo.
    ) else (
        echo   [4/4] No package.json, skipping Node.
        echo.
    )
)

rem --- 5. .env ---
rem     Use a variable to avoid CMD parser bug with dot-prefixed filenames in if-exist
set _ENV_EXISTS=0
if exist ".env" set _ENV_EXISTS=1
if %_ENV_EXISTS%==0 (
    if exist ".env.example" (
        copy .env.example .env >nul
        echo   Copied .env from .env.example. Edit .env to add API keys.
    )
) else (
    echo   .env already exists, not overwritten.
)
set _ENV_EXISTS=
echo.

echo   +--------------------------------------+
echo   ^|   Setup done.                      ^|
echo   ^|   Start: launch.bat  or  npm start ^|
echo   ^|   (CN: docs/SETUP_FIRST_RUN.zh.md)    ^|
echo   +--------------------------------------+
echo.
echo   Press any key to close this window...
pause >nul
