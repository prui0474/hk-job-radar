@echo off
chcp 65001 >nul
REM Run a full self-check (15 items) and tell you what still needs fixing.
REM Use this when something is not working, or before asking for help.
set "PYTHONUTF8=1"
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

"%PY%" run.py doctor

echo.
echo ---------------------------------------------
echo Items marked with a cross must be fixed; each
echo one tells you exactly what to change.
echo Screenshot this window if you need help.
echo ---------------------------------------------
pause
