@echo off
setlocal

echo ============================================================
echo  GPT-SoVITS API Quick Launcher
echo  Shikigami Protocol
echo ============================================================
echo.

rem 1) Resolve GPT-SoVITS directory (set via UI as GPTSOVITS_DIR)
set "TOOLS_DIR="
if defined GPTSOVITS_DIR (
    set "TOOLS_DIR=%GPTSOVITS_DIR%"
) else (
    set "TOOLS_DIR=%~dp0..\tools\GPT-SoVITS"
)

if not exist "%TOOLS_DIR%\api_v2.py" (
    echo [ERROR] Could not find api_v2.py in "%TOOLS_DIR%".
    echo        Please configure the GPT-SoVITS directory in Shikigami settings.
    echo.
    pause
    endlocal
    exit /b 1
)

echo [INFO] Using GPT-SoVITS directory: "%TOOLS_DIR%"
echo.

rem 2) Choose Python interpreter
set "PYTHON_CMD=python"
if exist "%TOOLS_DIR%\runtime\python.exe" (
    set "PYTHON_CMD=%TOOLS_DIR%\runtime\python.exe"
)

echo [INFO] Using Python: "%PYTHON_CMD%"
echo.

rem 3) Ensure required Python packages
"%PYTHON_CMD%" -c "import soundfile" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Python package 'soundfile' is missing.
    echo [INFO] Installing: soundfile
    "%PYTHON_CMD%" -m pip install soundfile
)

echo.
echo [INFO] Launching GPT-SoVITS API v2 ...
echo        Dir : "%TOOLS_DIR%"
echo        Note: keep this window open while using gpt_sovits TTS.
echo.

pushd "%TOOLS_DIR%"
"%PYTHON_CMD%" api_v2.py
popd

echo.
echo [INFO] GPT-SoVITS process exited.
echo.
pause
endlocal