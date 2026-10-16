#!/bin/bash
# ============================================================================
#  香港岗位雷达 · macOS 一键安装（双击运行，只需一次）
# ============================================================================
#  对应 Windows 的 install.bat。做的事完全一样：
#    检查 Python → 建虚拟环境 → 装依赖 → 装 Chromium 内核 → 生成配置
#    → 收集 DeepSeek key → 设置每天 09:00 自动运行
#
#  和 .bat 的区别：这里敢写中文（macOS 终端天生 UTF-8，没有 cmd.exe 那种
#  非 ASCII 解析问题），对用户更友好。
# ============================================================================

set -u

# 双击 .command 时当前目录是用户主目录，必须先切到脚本自己所在的目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=scripts/common.sh
. "$SCRIPT_DIR/scripts/common.sh"

cd "$RADAR_ROOT" || { echo "无法进入项目目录：$RADAR_ROOT"; exit 1; }
export PYTHONUTF8=1

radar_banner "香港岗位雷达 · Mac 一键安装（只需运行一次）"

# ---------------------------------------------------------------------------
# 第 0 步：解除「从网上下载」的隔离标记
#   macOS 会给从浏览器/微信下载的文件打上 com.apple.quarantine 标记并拦截
#   它们运行。这一步顺手清掉，免得用户后面被「无法验证开发者」反复拦住。
# ---------------------------------------------------------------------------
if xattr -r "$RADAR_ROOT" 2>/dev/null | grep -q '^com\.apple\.quarantine$'; then
    radar_info "检测到「从网上下载」的安全隔离标记，正在解除..."
    xattr -dr com.apple.quarantine "$RADAR_ROOT" 2>/dev/null || true
    radar_ok "已解除（这是 macOS 的常规保护，不是病毒）"
fi

# ---------------------------------------------------------------------------
# 第 1 步：找 Python 3.10+
# ---------------------------------------------------------------------------
PY3="$(radar_find_python3)" || PY3=""
if [ -z "$PY3" ]; then
    radar_err "这台电脑上没找到 Python 3.10 或更高版本。"
    echo
    echo "  Python 是运行本程序的「底座」，必须先装一个。二选一："
    echo
    echo "  【方式一 · 推荐】官网安装包（最简单，不用敲命令）"
    echo "     1. 用浏览器打开：https://www.python.org/downloads/macos/"
    echo "     2. 点最上面那个「Download Python 3.x.x」黄色按钮（macOS installer）"
    echo "     3. 双击下载好的 .pkg 文件，一路点「继续」「同意」「安装」"
    echo "        （中途会要你输入开机密码，属于正常）"
    echo "     4. 装完后重新双击本文件即可"
    echo
    echo "  【方式二 · 已经装了 Xcode 的人】在「终端」里运行："
    echo "     xcode-select --install"
    echo
    echo "  卡在这一步了？把整个窗口截图发给我，我告诉你怎么办。"
    radar_pause
    exit 1
fi
radar_ok "找到 Python：$PY3"
"$PY3" -c 'import sys; print("     版本 " + ".".join(map(str, sys.version_info[:3])))'

# ---------------------------------------------------------------------------
# 第 2 步：创建虚拟环境
#   虚拟环境 = 一个只属于本项目的、独立的 Python 小房间。好处是装的库
#   不会和你电脑上别的东西互相干扰，以后删掉整个文件夹就等于卸载干净。
# ---------------------------------------------------------------------------
echo
if [ -x "$RADAR_ROOT/.venv/bin/python" ]; then
    echo "[2/5] 虚拟环境已存在，跳过"
else
    echo "[2/5] 正在创建虚拟环境（约 10 秒）..."
    "$PY3" -m venv "$RADAR_ROOT/.venv"
    if [ ! -x "$RADAR_ROOT/.venv/bin/python" ]; then
        radar_err "虚拟环境创建失败。请把整个窗口截图发给我。"
        radar_pause
        exit 1
    fi
    # 少数精简版 Python 建出来的环境没带 pip，补一下
    if ! "$RADAR_ROOT/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
        radar_warn "虚拟环境里缺少 pip，正在自动修复..."
        "$RADAR_ROOT/.venv/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
    fi
fi

VENV_PY="$RADAR_ROOT/.venv/bin/python"
if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    radar_err "虚拟环境不完整（pip 不可用）。删掉项目里的 .venv 文件夹后重跑本文件。"
    radar_pause
    exit 1
fi

# ---------------------------------------------------------------------------
# 第 3 步：安装依赖
#   先走官方源；失败（国内网络常见）自动换成清华镜像重试一次。
# ---------------------------------------------------------------------------
echo
echo "[3/5] 正在安装依赖（首次约 1-3 分钟，请勿关闭窗口）..."
if ! "$VENV_PY" -m pip install --disable-pip-version-check \
        -r "$RADAR_ROOT/requirements.txt"; then
    radar_warn "官方源失败，自动改用清华镜像重试..."
    if ! "$VENV_PY" -m pip install --disable-pip-version-check \
            -i https://pypi.tuna.tsinghua.edu.cn/simple \
            -r "$RADAR_ROOT/requirements.txt"; then
        radar_err "依赖安装失败。请检查网络连接后重新双击本文件。"
        radar_pause
        exit 1
    fi
fi
radar_ok "依赖安装完成"

# ---------------------------------------------------------------------------
# 第 4 步：下载 Chromium 内核
#   抓取网页要用一个真实的浏览器，Playwright 会下载它自己的 Chromium
#   （约 150MB，只下一次，以后复用）。
# ---------------------------------------------------------------------------
echo
echo "[4/5] 正在下载浏览器内核 Chromium（首次约 1-3 分钟）..."
if ! "$VENV_PY" -m playwright install chromium; then
    radar_warn "默认源下载失败，自动改用国内镜像重试..."
    if ! PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/ \
         "$VENV_PY" -m playwright install chromium; then
        radar_err "浏览器内核下载失败。请检查网络后重新双击本文件。"
        radar_pause
        exit 1
    fi
fi
radar_ok "浏览器内核已就绪"

# ---------------------------------------------------------------------------
# 第 5 步：生成配置文件
#   已存在的文件一律不覆盖——里面可能有用户自己填的内容。
# ---------------------------------------------------------------------------
echo
if [ -f "$RADAR_ROOT/config.yaml" ]; then
    echo "[5/5] config.yaml 已存在，保留你的设置"
else
    cp "$RADAR_ROOT/config.example.yaml" "$RADAR_ROOT/config.yaml"
    echo "[5/5] 已生成 config.yaml"
fi
mkdir -p "$RADAR_ROOT/data/profiles" "$RADAR_ROOT/data/logs"
if [ -f "$RADAR_ROOT/data/profiles/profile.yaml" ]; then
    radar_info "data/profiles/profile.yaml 已存在，跳过"
elif [ -f "$RADAR_ROOT/data/profiles/profile.example.yaml" ]; then
    cp "$RADAR_ROOT/data/profiles/profile.example.yaml" \
       "$RADAR_ROOT/data/profiles/profile.yaml"
    radar_info "已生成 data/profiles/profile.yaml"
fi

# ---------------------------------------------------------------------------
# DeepSeek API key（用于 AI 给岗位打分）
# ---------------------------------------------------------------------------
echo
if [ -f "$RADAR_ROOT/api_key.txt" ]; then
    radar_ok "DeepSeek key 之前已保存过，跳过"
elif [ -t 0 ] && [ -z "${CI:-}" ]; then
    echo "------------------------------------------------------------"
    echo " 还差一件事：DeepSeek API key（用来让 AI 给岗位打分）"
    echo
    echo " 还没有？打开 https://platform.deepseek.com 注册登录，"
    echo " 左侧点「API Keys」→「创建」，复制那串 sk- 开头的文字。"
    echo "------------------------------------------------------------"
    echo
    printf '%s' "把 key 粘贴到这里，然后按回车（现在不想填就直接回车跳过）："
    read -r USERKEY || USERKEY=""
    if [ -n "${USERKEY:-}" ]; then
        printf '%s\n' "$USERKEY" > "$RADAR_ROOT/api_key.txt"
        chmod 600 "$RADAR_ROOT/api_key.txt" 2>/dev/null || true
        radar_ok "已保存到 api_key.txt（只存在你自己电脑上，不会上传）"
    else
        radar_info "已跳过。以后可以直接新建 api_key.txt 把 key 写进去。"
    fi
else
    radar_info "已跳过 key 填写（非交互式运行）"
fi

# ---------------------------------------------------------------------------
# 每天 09:00 自动运行（macOS 的 launchd，相当于 Windows 的任务计划）
# ---------------------------------------------------------------------------
if [ -z "${CI:-}" ] && [ -t 0 ]; then
    echo
    echo "正在设置「每天 09:00 自动抓一轮」..."
    if radar_launchd_install "$VENV_PY" "$RADAR_ROOT"; then
        radar_ok "已设置成功。电脑当时在睡觉/关机也没关系，醒来会补跑一次。"
        radar_info "不想要了？双击项目里的「关闭自动运行.command」即可取消。"
    else
        radar_warn "自动运行没设置成功（不影响使用，双击 run_once.command 照样能跑）"
    fi
fi

# ---------------------------------------------------------------------------
# 完成
# ---------------------------------------------------------------------------
echo
echo "============================================"
echo " 安装完成！还剩 4 件小事："
echo
echo "  1. 双击「配置」相关的文件，或直接用记事本打开 config.yaml，填好："
echo "       - Telegram 的 bot_token / chat_id"
echo "         （在 Telegram 里找 @BotFather 建一个机器人，它会给你 token）"
echo "       - 把 keywords 换成你自己想找的岗位名称"
echo "  2. 打开 data/profiles/profile.yaml，填上你自己的背景。"
echo "     AI 就是拿这份画像给岗位打分的 —— 这一步千万别跳过。"
echo "     省力办法：把你的简历贴给 DeepSeek，让它照着模板帮你填。"
echo "  3. 第一次使用请双击一次「login_jobsdb.command」（过一下网站的安全检查）"
echo "  4. 双击「start.command」开始监控；或者双击「run_once.command」只跑一轮"
echo
echo " 完整图文教程（中文，一步一步点）：docs/getting-started-zh.md"
echo " Mac 专用教程：                docs/mac-安装教程.md"
echo "============================================"
radar_pause
