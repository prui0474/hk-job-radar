@echo off
chcp 65001 >nul
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

echo A browser window will open.
echo Pass the JobsDB security check, then come back here and press Enter.
echo.

"%PY%" run.py login jobsdb

echo.
pause
