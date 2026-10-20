@echo off
chcp 65001 >nul
REM Force Python into UTF-8 mode. On Simplified-Chinese Windows the default
REM preferred encoding is GBK (cp936), which can break file/console decoding.
set "PYTHONUTF8=1"
setlocal
cd /d "%~dp0"
title HK Job Radar - Setup

REM ---------------------------------------------------------------------------
REM MAINTAINER NOTE: this file must stay pure ASCII.
REM cmd.exe parses .bat files with the system code page, so Chinese written
REM here gets mangled (or worse, a stray byte gets read as a command
REM separator). Every long Chinese explanation is therefore printed by
REM "run.py guide <key>" instead - Python's stdout is UTF-8 and safe.
REM Only short English step headers live in this file.
REM ---------------------------------------------------------------------------

echo ============================================
echo   HK Job Radar - One-click setup (run once)
echo ============================================
echo.
echo This window will walk you all the way to a
echo running system. Please keep it open.
echo.

REM ---- 1/7. Check Python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo [X] Python was not found on this computer.
    echo.
    echo     Please install Python first:
    echo       1. Open https://www.python.org/downloads/
    echo       2. Click the yellow "Download Python" button
    echo       3. Run the installer and TICK the checkbox
    echo          "Add python.exe to PATH" at the BOTTOM
    echo       4. Then double-click this file again
    echo.
    echo     Chinese step-by-step guide: docs\getting-started-zh.md
    echo.
    if not defined CI pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version') do echo [1/7] Found %%v
echo.

REM ---- 2/7. Create virtual environment ----
if exist ".venv\Scripts\python.exe" (
    echo [2/7] Virtual environment already exists - skipping.
) else (
    echo [2/7] Creating virtual environment ^(about 10s^)...
    python -m venv .venv
    if errorlevel 1 (
        echo [X] Failed to create the virtual environment.
        echo     Screenshot this window and ask for help.
        if not defined CI pause
        exit /b 1
    )
)
echo.

REM ---- 3/7. Install dependencies ----
echo [3/7] Installing dependencies ^(1-3 min on first run^)...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [X] Dependency installation failed.
    echo     Check your network connection, then run this file again.
    if not defined CI pause
    exit /b 1
)
echo.

REM ---- 4/7. Install Chromium browser engine ----
echo [4/7] Preparing the Chromium browser engine ^(1-3 min^)...

REM Some older systems cannot run the bundled runner of a recent playwright
REM build (it dies with "Symbol not found" / dyld). Probe it first, and if it
REM cannot even print a version, fall back to an older release BEFORE
REM downloading 150 MB - otherwise the failure looks like a network problem.
".venv\Scripts\python.exe" -m playwright --version >nul 2>&1
if errorlevel 1 (
    echo       This build of the browser tool cannot run on your system.
    echo       Trying an older, compatible version automatically...
    call :try_playwright_pin 1.45.0
    if errorlevel 1 call :try_playwright_pin 1.44.0
    if errorlevel 1 call :try_playwright_pin 1.43.1
    if errorlevel 1 (
        echo [X] Could not find a browser tool version that runs here.
        echo     This usually means the OS is older than recent builds support.
        echo     Updating the system, then running this file again, is the safest fix.
        if not defined CI pause
        exit /b 1
    )
    echo       Switched to a compatible playwright release.
)

".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
    echo [X] Browser engine installation failed.
    echo     Check your network connection, then run this file again.
    if not defined CI pause
    exit /b 1
)
echo.

REM ---- 5/7. Generate config files ----
REM FRESH=1 means config.yaml did not exist yet, i.e. this is a first install.
REM Only a first install walks the user through the wizard and the login step;
REM re-running this file later must just repair the environment and leave the
REM user's own settings alone.
set "FRESH=0"
if not exist config.yaml set "FRESH=1"

if exist config.yaml (
    echo [5/7] config.yaml already exists - skipping ^(your settings are kept^).
) else (
    copy config.example.yaml config.yaml >nul
    echo [5/7] Created config.yaml.
)
if exist "data\profiles\profile.yaml" (
    echo       profile.yaml already exists - skipping.
) else (
    copy "data\profiles\profile.example.yaml" "data\profiles\profile.yaml" >nul 2>nul
    echo       Created data\profiles\profile.yaml.
)

REM ---- Collect DeepSeek key (stored locally, gitignored) ----
if exist api_key.txt (
    echo [OK] DeepSeek key already saved - skipping.
    goto after_key
)
echo.
echo ------------------------------------------------------------
echo  One more thing: the DeepSeek API key ^(used for AI scoring^)
echo.
echo  No key yet? Open https://platform.deepseek.com
echo  Sign up, go to "API Keys", create one, copy the "sk-..." string
echo ------------------------------------------------------------
echo.
set "USERKEY="
set /p USERKEY=Paste your key here and press Enter (or press Enter to skip): 
if not defined USERKEY goto after_key
>api_key.txt echo %USERKEY%
echo [OK] Key saved to api_key.txt ^(never uploaded anywhere^).
:after_key
echo.

REM ---- 6/7. Configuration wizard (first install only) ----
REM Everything from here to :finish is skipped in CI: there is no human to
REM click through the wizard or to fix what the self-check reports, so those
REM steps would either hang the build or fail it for the wrong reason.
if defined CI goto chain_skipped
if not "%FRESH%"=="1" goto after_wizard

echo [6/7] Opening the configuration wizard in your browser...
echo       Fill in the form, click "Save", then click "Close wizard".
echo       This window is waiting for you - please do not close it.
echo.
".venv\Scripts\python.exe" run.py setup --chain
if errorlevel 3 goto wizard_unsaved
if errorlevel 1 goto wizard_failed
echo.
echo       Settings saved.
:after_wizard

REM ---- 7/7. Self-check ----
if exist api_key.txt set /p DEEPSEEK_API_KEY=<api_key.txt
echo.
echo [7/7] Running the self-check ^(15 items^)...
echo.
".venv\Scripts\python.exe" run.py doctor
if errorlevel 1 goto doctor_failed
goto maybe_verify

:wizard_unsaved
".venv\Scripts\python.exe" run.py guide need-save
if not defined CI pause
exit /b 3

:wizard_failed
".venv\Scripts\python.exe" run.py guide wizard-failed
if not defined CI pause
exit /b 1

:doctor_failed
".venv\Scripts\python.exe" run.py guide doctor-failed
if not defined CI pause
exit /b 1

:maybe_verify
REM First install only: pass the JobsDB security check once, while the user is
REM still sitting in front of the screen. Re-runs must not pop a browser.
if not "%FRESH%"=="1" goto finish
".venv\Scripts\python.exe" run.py guide verify-intro
".venv\Scripts\python.exe" run.py login jobsdb
if errorlevel 1 ".venv\Scripts\python.exe" run.py guide verify-skip
goto finish

:chain_skipped
:finish
if not "%FRESH%"=="1" goto finish_repeat
".venv\Scripts\python.exe" run.py guide done
goto finish_pause
:finish_repeat
".venv\Scripts\python.exe" run.py guide repeat-done
:finish_pause
echo.
if not defined CI pause
exit /b 0

REM ---- helper: install one pinned playwright release and verify it runs ----
:try_playwright_pin
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q "playwright==%~1" >nul 2>&1
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m playwright --version >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0
