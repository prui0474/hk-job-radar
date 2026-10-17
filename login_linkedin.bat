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

echo ============================================================
echo   Log in to LinkedIn - pick one way
echo ============================================================
echo.
echo   1 = Open a browser window and log in there.
echo       Try this first. A small dedicated account is recommended.
echo.
echo   2 = Copy the login cookie from your own browser.
echo       Use this when the verification box in the opened window
echo       keeps spinning and never lets you through.
echo.
set "MODE="
set /p "MODE=Type 1 or 2, then press Enter (blank = 1): "

if "%MODE%"=="2" goto cookie
"%PY%" run.py login linkedin
echo.
pause
exit /b 0

:cookie
"%PY%" run.py login linkedin --cookie
echo.
pause
exit /b 0
