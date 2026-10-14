@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 香港岗位雷达 - 一键安装

echo ============================================
echo   香港岗位雷达 - 一键安装（只需运行一次）
echo ============================================
echo.

REM ---- 1. 检查 Python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo [X] 没有检测到 Python。
    echo.
    echo     请先安装 Python：
    echo     1. 浏览器打开 https://www.python.org/downloads/
    echo     2. 点击黄色的 Download Python 按钮下载
    echo     3. 运行安装程序，务必勾选底部的
    echo        "Add python.exe to PATH"
    echo     4. 装好后重新双击本文件
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version') do echo [1/5] 已检测到 %%v
echo.

REM ---- 2. 创建虚拟环境 ----
if exist ".venv\Scripts\python.exe" (
    echo [2/5] 虚拟环境已存在，跳过创建。
) else (
    echo [2/5] 正在创建虚拟环境（约 10 秒，请勿关闭窗口）...
    python -m venv .venv
    if errorlevel 1 (
        echo [X] 虚拟环境创建失败，请截图本窗口发给我。
        pause
        exit /b 1
    )
)
echo.

REM ---- 3. 安装依赖 ----
echo [3/5] 正在安装依赖（第一次约 1-3 分钟，取决于网速）...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [X] 依赖安装失败：请检查网络后重新双击本文件重试。
    pause
    exit /b 1
)
echo.

REM ---- 4. 安装浏览器内核 ----
echo [4/5] 正在安装 Chromium 浏览器内核（约 1-3 分钟）...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
    echo [X] 浏览器内核安装失败：请检查网络后重试。
    pause
    exit /b 1
)
echo.

REM ---- 5. 生成配置文件 ----
if exist config.yaml (
    echo [5/5] config.yaml 已存在，跳过（不会覆盖你的配置）。
) else (
    copy config.example.yaml config.yaml >nul
    echo [5/5] 已生成 config.yaml（还差 2 个空要填，见下方说明）。
)
if exist "data\profiles\profile.yaml" (
    echo       个人画像 profile.yaml 已存在，跳过。
) else (
    copy "data\profiles\profile.example.yaml" "data\profiles\profile.yaml" >nul 2>nul
    echo       已生成个人画像 profile.yaml。
)

REM ---- 收集 DeepSeek key（存到本地文件，已被 gitignore，不会上传）----
if exist api_key.txt (
    echo [OK] DeepSeek key 已保存过，跳过。
    goto after_key
)
echo.
echo ------------------------------------------------------------
echo  还差一步：DeepSeek API key（用于 AI 智能打分）
echo.
echo  还没有 key？用浏览器打开 https://platform.deepseek.com
echo  注册登录后，点左侧 API Keys - 创建 - 复制一串 sk- 开头的文字
echo ------------------------------------------------------------
echo.
set "USERKEY="
set /p USERKEY=把 key 粘贴到这里后按回车（还没有就先直接回车跳过）: 
if not defined USERKEY goto after_key
>api_key.txt echo %USERKEY%
echo [OK] key 已保存到 api_key.txt（此文件不会上传到 GitHub）。
:after_key

echo.
echo ============================================
echo   安装完成！接下来还剩 4 件小事：
echo.
echo   1. 记事本打开 config.yaml，填 2 行：
echo        bot_token: 去 Telegram 找 @BotFather 创建机器人
echo                   后复制粘贴
echo        chat_id:   给你的机器人发条消息，然后按
echo                   教程里的方法获取（很简单）
echo       （同一文件里，把 keywords 换成你自己要找的岗位！）
echo   2. 记事本打开 data\profiles\profile.yaml，
echo        填你自己的真实背景。这决定 AI 打分准不准，
echo        别跳过。填法：把简历贴给 DeepSeek，让它按
echo        模板格式生成（教程第 7 步有现成话术）。
echo   3. 双击 login_jobsdb.bat 完成一次防护验证
echo      （会弹出浏览器，点一下验证就行）
echo   4. 双击 start.bat 开始监控！
echo.
echo   超详细新手教程（每一步点哪里、看到什么）：
echo   用记事本打开 docs\getting-started-zh.md
echo ============================================
echo.
pause
