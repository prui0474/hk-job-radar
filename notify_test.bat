@echo off
chcp 65001 >nul
REM Send a test message to the push channel(s) you enabled.
REM Use this right after editing config.yaml -- if the message arrives,
REM your channel setup is correct.
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

"%PY%" run.py notify test

echo.
echo ---------------------------------------------
echo If a message arrived, your push channel works.
echo If not, check data\logs\ for the reason, or read
echo the push-channel guide under docs\ (Chinese).
echo ---------------------------------------------
pause
