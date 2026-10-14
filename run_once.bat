@echo off
chcp 65001 >nul
REM Force Python into UTF-8 mode. On Simplified-Chinese Windows the default
REM preferred encoding is GBK (cp936), which can break file/console decoding.
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

echo Running ONE sweep now.
echo A browser may open for JobsDB / LinkedIn / Indeed.
echo Pages are fetched with a 2-8s random delay, so please be patient.
echo The digest is pushed to Telegram at the end, then this window exits.
echo.

"%PY%" run.py once

echo.
echo ---------------------------------------------
echo Finished. If a digest was pushed you can close this window.
echo If you see a red error above, screenshot it and ask for help.
echo ---------------------------------------------
pause
