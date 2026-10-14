@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 香港岗位雷达 - 抓一轮（一次性）

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

echo ============================================
echo   香港岗位雷达 - 抓一轮就收工
echo ============================================
echo.
echo 正在抓取（会依次打开 JobsDB / LinkedIn / Indeed，
echo 每个页面之间有 2-8 秒随机间隔以模拟人工，请耐心等待）。
echo 跑完会自动推送日报到 Telegram，然后自己退出。
echo.

"%PY%" run.py once

echo.
echo ============================================
echo 本轮结束。看到上面有「日报：已推送」就说明成功，
echo 现在可以直接关掉这个窗口、甚至直接关机。
echo 如果上方有红色报错，请截图发给我。
echo ============================================
pause
