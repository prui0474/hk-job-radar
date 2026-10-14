@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title HK Job Radar - Setup

echo ============================================
echo   HK Job Radar - One-click setup (run once)
echo ============================================
echo.

REM ---- 1. Check Python ----
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
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version') do echo [1/5] Found %%v
echo.

REM ---- 2. Create virtual environment ----
if exist ".venv\Scripts\python.exe" (
    echo [2/5] Virtual environment already exists - skipping.
) else (
    echo [2/5] Creating virtual environment ^(about 10s^)...
    python -m venv .venv
    if errorlevel 1 (
        echo [X] Failed to create the virtual environment.
        echo     Screenshot this window and ask for help.
        pause
        exit /b 1
    )
)
echo.

REM ---- 3. Install dependencies ----
echo [3/5] Installing dependencies ^(1-3 min on first run^)...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [X] Dependency installation failed.
    echo     Check your network connection, then run this file again.
    pause
    exit /b 1
)
echo.

REM ---- 4. Install Chromium browser engine ----
echo [4/5] Installing Chromium browser engine ^(1-3 min^)...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
    echo [X] Browser engine installation failed.
    echo     Check your network connection, then run this file again.
    pause
    exit /b 1
)
echo.

REM ---- 5. Generate config files ----
if exist config.yaml (
    echo [5/5] config.yaml already exists - skipping ^(your settings are kept^).
) else (
    copy config.example.yaml config.yaml >nul
    echo [5/5] Created config.yaml.
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
echo ============================================
echo  Setup complete! 4 small things left:
echo.
echo   1. Open config.yaml in Notepad and fill in:
echo        - bot_token / chat_id  (create a bot via @BotFather on Telegram)
echo        - replace "keywords" with YOUR OWN target job titles
echo   2. Open data\profiles\profile.yaml in Notepad and fill in
echo        YOUR OWN background. This is what the AI scores jobs against
echo        - please do not skip it.
echo        Tip: paste your CV into DeepSeek and ask it to fill the
echo        template for you (see the guide, step 7).
echo   3. Double-click login_jobsdb.bat once (security check).
echo   4. Double-click start.bat to begin monitoring.
echo.
echo  Full beginner guide (Chinese, click-by-click):
echo    open docs\getting-started-zh.md in Notepad
echo ============================================
echo.
pause
