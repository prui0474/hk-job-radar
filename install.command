#!/bin/bash
# ============================================================================
#  香港岗位雷达 · macOS 一键安装（双击运行，只需一次）
# ============================================================================
#  对应 Windows 的 install.bat。做的事完全一样：
#    检查 Python → 建虚拟环境 → 装依赖 → 装 Chromium 内核 → 生成配置
#    → 收集 DeepSeek key → 设置自动运行 → 配置向导 → 体检 → 过一次验证
#
#  也就是说：用户只需要双击这一次，后面每一步都由本脚本接着做，
#  不用自己判断「现在该点哪个文件」。
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
    echo "[2/7] 虚拟环境已存在，跳过"
elif [ -x "$RADAR_ROOT/.venv/Scripts/python.exe" ]; then
    # 这个 .venv 是在 Windows 上建的（Mac 的虚拟环境结构完全不同：bin/ 而不是 Scripts/）。
    # 有人可能是把整个项目文件夹从 Windows 拷到 Mac 才遇到这种情况。此时直接
    # 重建会得到一个"半 Windows 半 Mac"的坏环境，报错还特别难懂，所以宁可停下来
    # 说清楚该怎么办，也不要硬着头皮往下跑。
    radar_err "检测到项目里的 .venv 是在 Windows 上创建的，Mac 上没法用。"
    echo
    echo "  请这样做："
    echo "   1. 在「访达」里打开本项目文件夹"
    echo "   2. 找到 .venv 这个文件夹，把它拖进废纸篓"
    echo "      （它是虚拟环境，删掉不会丢任何数据，重跑本文件会自动重建）"
    echo "   3. 重新双击 install.command"
    echo
    echo "  看不到 .venv？按 Command + Shift + .（句号）显示隐藏文件。"
    radar_pause
    exit 1
else
    echo "[2/7] 正在创建虚拟环境（约 10 秒）..."
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
echo "[3/7] 正在安装依赖（首次约 1-3 分钟，请勿关闭窗口）..."
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
echo "[4/7] 正在准备浏览器内核 Chromium（首次约 1-3 分钟）..."

# ---------------------------------------------------------------------------
# 先确认 playwright 自带的运行器能不能在这台机器上跑起来。
#   旧版 macOS（低于 13.5）跑新版本 playwright 时，它内置的 node 运行器会直接
#   因为找不到新版 libc++ 的符号而崩溃（报 Symbol not found / dyld ... built for
#   Mac OS X 13.5）。这不是网络问题，换镜像重试多少次都一样 —— 所以必须先降
#   版本，再去下载内核，否则用户会被「请检查网络」这句提示带到沟里。
# ---------------------------------------------------------------------------
if ! "$VENV_PY" -m playwright --version >/dev/null 2>&1; then
    radar_warn "当前版本的浏览器工具无法在你的系统上运行（常见于 macOS 13.5 以下）"
    echo "      正在自动改用兼容版本，这个不用你操作，稍等就好..."
    PW_PINNED=""
    for pin in 1.45.0 1.44.0 1.43.1; do
        echo "      尝试 playwright==$pin ..."
        if "$VENV_PY" -m pip install --disable-pip-version-check -q \
                "playwright==$pin" >/dev/null 2>&1 \
           && "$VENV_PY" -m playwright --version >/dev/null 2>&1; then
            PW_PINNED="$pin"
            break
        fi
    done
    if [ -z "$PW_PINNED" ]; then
        radar_err "没能在你的系统上找到可用的浏览器工具版本。"
        echo
        echo "  你的 macOS 版本：$(sw_vers -productVersion 2>/dev/null || echo 未知)"
        echo "  最省事的办法是把系统升级到 13.5 以上，再重新双击本文件："
        echo "    苹果菜单  →  系统设置  →  通用  →  软件更新"
        radar_pause
        exit 1
    fi
    radar_ok "已改用兼容版本 playwright $PW_PINNED"
fi

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
# FRESH=1 表示 config.yaml 是这一次才生成的，也就是「第一次安装」。
# 只有第一次安装才带用户走配置向导、才自动过一次人机验证；
# 以后重复双击本文件，只修环境，绝不碰用户自己的设置。
FRESH=0
[ -f "$RADAR_ROOT/config.yaml" ] || FRESH=1

# INTERACTIVE=1 表示真的有人正看着这个窗口。
# 判据与 radar_pause 完全一致：有终端 + 不是 CI。
#
# 为什么后面几步都要靠它把关：配置向导会一直等人操作，体检又必然因为
# CI 里没有密钥/没有配置而报 ❌ —— 两者都会让 CI 里的 ./install.command
# 卡死或变红。有真人在时这些步骤才有意义。
INTERACTIVE=0
if [ -t 0 ] && [ -z "${CI:-}" ]; then
    INTERACTIVE=1
fi

echo
if [ -f "$RADAR_ROOT/config.yaml" ]; then
    echo "[5/7] config.yaml 已存在，保留你的设置"
else
    cp "$RADAR_ROOT/config.example.yaml" "$RADAR_ROOT/config.yaml"
    echo "[5/7] 已生成 config.yaml"
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
# 第 6 步：配置向导（只在第一次安装时走）
#   两个跳过条件：
#     1) config.yaml 之前就存在 —— 用户已经配过了，不该再打扰他
#     2) 没有真人在看窗口（CI）—— 向导要等人在浏览器里点，非交互环境下
#        它会一直挂着，把整个构建卡死
# ---------------------------------------------------------------------------
if [ "$FRESH" = "1" ] && [ "$INTERACTIVE" = "1" ]; then
    echo
    echo "[6/7] 正在打开配置向导（浏览器窗口会自动弹出）..."
    echo "      填完点最下面的「保存配置」，再点「关闭向导」——"
    echo "      这个窗口在等你，请不要关掉它。"
    echo
    "$VENV_PY" "$RADAR_ROOT/run.py" setup --chain
    SETUP_CODE=$?
    if [ "$SETUP_CODE" -eq 3 ]; then
        # 3 = 向导关了但一次都没保存过。往下体检毫无意义，停下来把话说清楚。
        "$VENV_PY" "$RADAR_ROOT/run.py" guide need-save
        radar_pause
        exit 3
    elif [ "$SETUP_CODE" -ne 0 ]; then
        "$VENV_PY" "$RADAR_ROOT/run.py" guide wizard-failed
        radar_pause
        exit 1
    fi
    radar_ok "配置已保存"
fi

# ---------------------------------------------------------------------------
# 第 7 步：体检
#   15 项逐条检查。只要有一项 ❌ 就停下来 —— 带着坏配置往下走，用户
#   只会更晚、更困惑地撞上同一个问题。
#
#   没有真人在场时直接跳过：CI 里的配置是模板复制出来的空壳（没有密钥、
#   没有推送通道），体检必然报 ❌。那不是代码坏了，是还没人配置过。
# ---------------------------------------------------------------------------
if [ "$INTERACTIVE" = "1" ]; then
    echo
    echo "[7/7] 正在体检（15 项）..."
    echo
    if ! "$VENV_PY" "$RADAR_ROOT/run.py" doctor; then
        "$VENV_PY" "$RADAR_ROOT/run.py" guide doctor-failed
        radar_pause
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# 最后一步：过一次 JobsDB 的人机验证
#   体检全绿才走这里，而且只有第一次安装才走 —— 用户此刻正坐在屏幕前，
#   是让他顺手过一次验证的最好时机，比事后再让他去双击另一个文件强。
# ---------------------------------------------------------------------------
if [ "$FRESH" = "1" ] && [ "$INTERACTIVE" = "1" ]; then
    echo
    "$VENV_PY" "$RADAR_ROOT/run.py" guide verify-intro
    if ! "$VENV_PY" "$RADAR_ROOT/run.py" login jobsdb; then
        "$VENV_PY" "$RADAR_ROOT/run.py" guide verify-skip
    fi
fi

# ---------------------------------------------------------------------------
# 完成
# ---------------------------------------------------------------------------
echo
if [ "$FRESH" = "1" ]; then
    "$VENV_PY" "$RADAR_ROOT/run.py" guide done
else
    "$VENV_PY" "$RADAR_ROOT/run.py" guide repeat-done
fi
radar_pause
