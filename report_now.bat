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

"%PY%" run.py report now

echo.
echo ---------------------------------------------
echo Digest push attempted.
echo If it says "no new jobs to push", run start.bat first.
echo Screenshot any red error above and ask for help.
echo ---------------------------------------------
pause
