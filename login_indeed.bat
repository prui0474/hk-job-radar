@echo off
chcp 65001 >nul
REM Force Python into UTF-8 mode. On Simplified-Chinese Windows the default
REM preferred encoding is GBK (cp936), which can break file/console decoding.
set "PYTHONUTF8=1"
cd /d "%~dp0"

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist ".venv_test\Scripts\python.exe" set "PY=.venv_test\Scripts\python.exe"
if not defined PY (
    echo [X] Python environment not found.
    echo     Please run install.bat first, then try again.
    pause
    exit /b 1
)

echo Indeed has no account to log in to. What it does have is a security
echo check, and it insists on a visible browser window.
echo.
echo Note: because of that, a normal run will also pop up a browser window
echo when it reaches Indeed. This script is only for checking the site once
echo on purpose, to see whether the page loads on your network.
echo.
"%PY%" run.py login indeed
echo.
pause
