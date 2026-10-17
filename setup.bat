@echo off
chcp 65001 >nul
REM Open the configuration wizard in your browser.
REM Fill in the form, click Save, then click "Close wizard".
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

"%PY%" run.py setup

echo.
echo ---------------------------------------------
echo Wizard closed.
echo If you already saved the settings, just
echo double-click start.bat to begin.
echo ---------------------------------------------
pause
