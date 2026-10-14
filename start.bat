@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 香港岗位雷达

if exist api_key.txt set /p DEEPSEEK_API_KEY=<api_key.txt

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist ".venv_test\Scripts\python.exe" set "PY=.venv_test\Scripts\python.exe"
if not defined PY (
    echo 还没有安装运行环境。
    echo 请先双击 install.bat 完成一键安装（只需一次），再回来双击本文件。
    pause
    exit /b 1
)

"%PY%" run.py run
echo.
echo ============================================
echo 程序已停止。如果上方有红色报错，请截图发给我。
echo ============================================
pause
