@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist api_key.txt set /p DEEPSEEK_API_KEY=<api_key.txt

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist ".venv_test\Scripts\python.exe" set "PY=.venv_test\Scripts\python.exe"
if not defined PY (
    echo [X] Python environment not found.
    echo     Please run install.bat first, then try again.
    pause
    exit /b 1
)

echo Starting the monitor. Keep this window open while it runs.
echo Press Ctrl+C or close this window to stop.
echo.

"%PY%" run.py run

echo.
pause
