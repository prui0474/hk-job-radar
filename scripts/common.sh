#!/bin/bash
# ============================================================================
#  hk_job_radar · macOS / Linux 通用脚本库
# ============================================================================
#  所有 *.command 脚本都 source 本文件，把「找项目根目录 / 找 Python / 打印
#  彩色提示 / 暂停窗口 / 写 launchd 定时任务」这些公共逻辑集中在一处，避免
#  7 个脚本各写一份、改一处漏一处。
#
#  两条必须遵守的约定（改这个文件前请先读）：
#   1. 只用 bash 3.2 兼容语法。macOS 自带的 /bin/bash 就是 3.2（2007 年版本），
#      不支持 ${var,,}、declare -A（关联数组）等 4.x 才有的写法；
#   2. 本文件必须保持 UTF-8 无 BOM + LF 换行。带 BOM 会让 shebang 失效，
#      CRLF 会让 bash 报 "$'\r': command not found"。.gitattributes 已锁定。
#
#  为什么这里的提示是中文，而 Windows 的 .bat 全是英文：
#    不是双标，是技术限制。cmd.exe 解析批处理时对非 ASCII 字符极不可靠
#    （中文会被当成命令执行，报"不是内部或外部命令"），所以 .bat 只能写英文；
#    macOS 终端天生 UTF-8，没有任何限制，所以这里直接说中文对用户更友好。
# ============================================================================

# ---------------------------------------------------------------------------
# 定位项目根目录
#   双击 .command 时，macOS 会把「当前工作目录」设成用户主目录（~）而不是
#   脚本所在目录，所以绝不能用 $(pwd) 推路径，必须从脚本自身位置反推。
# ---------------------------------------------------------------------------
_radar_self_path() {
    local p="${BASH_SOURCE[0]:-$0}"
    case "$p" in
        /*) printf '%s\n' "$p" ;;
        *)  printf '%s/%s\n' "$(pwd)" "$p" ;;
    esac
}

radar_project_root() {
    local self dir
    self="$(_radar_self_path)"
    dir="$(dirname "$self")"          # .../项目根/scripts
    ( cd "$dir/.." && pwd )           # 用子 shell，避免污染调用方的 cwd
}

RADAR_ROOT="$(radar_project_root)"
export RADAR_ROOT

# ---------------------------------------------------------------------------
# 彩色输出
# ---------------------------------------------------------------------------
radar_ok()   { printf '  \033[32m[OK]\033[0m %s\n' "$*"; }
radar_info() { printf '  %s\n' "$*"; }
radar_warn() { printf '  \033[33m[!]\033[0m %s\n' "$*"; }
radar_err()  { printf '  \033[31m[X]\033[0m %s\n' "$*"; }

radar_banner() {
    echo "============================================"
    printf '  %s\n' "$1"
    echo "============================================"
    echo
}

# ---------------------------------------------------------------------------
# 暂停：等用户按键
#   只在「交互式终端」里停住；在 CI / 管道 / 重定向场景下直接返回，
#   否则自动化测试会永远卡在这一行。
# ---------------------------------------------------------------------------
radar_pause() {
    if [ -t 0 ] && [ -z "${CI:-}" ]; then
        printf '\n%s' "按回车键关闭这个窗口 ... "
        read -r _radar_dummy || true
        printf '\n'
    fi
}

# ---------------------------------------------------------------------------
# 找项目内的虚拟环境 Python
#   macOS/Linux 在 .venv/bin/python，Windows 在 .venv/Scripts/python.exe。
#   本库同时认这两种，方便从 Windows 打包/迁移过来的目录直接可用。
# ---------------------------------------------------------------------------
radar_find_python() {
    local c
    for c in "$RADAR_ROOT/.venv/bin/python" \
             "$RADAR_ROOT/.venv/Scripts/python.exe" \
             "$RADAR_ROOT/.venv_test/bin/python" \
             "$RADAR_ROOT/.venv_test/Scripts/python.exe"; do
        if [ -x "$c" ]; then
            printf '%s\n' "$c"
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# 找系统里可用的 Python 3（>= 3.10），用于「创建虚拟环境」这一步
#
# 这是 Mac 上最大的坑，所以探测顺序刻意做得很讲究：
#   macOS 从 12.3 起不再自带 Python，/usr/bin/python3 是个「Xcode 命令行工具
#   的壳」——没装 CLT 时运行它会弹出一个图形安装框并卡住。所以：
#     ① 先找 python.org 官方安装包（/Library/Frameworks/...）
#     ② 再找 Homebrew（Apple 芯片 /opt/homebrew、Intel /usr/local）
#     ③ 再找 PATH 里的 python3
#     ④ 最后才考虑 /usr/bin/python3，且必须先用 xcode-select -p 确认 CLT 已装
# ---------------------------------------------------------------------------
radar_find_python3() {
    # 刻意不用数组：macOS 自带的 bash 3.2 在 `set -u` 下对空数组求
    # ${#arr[@]} / ${arr[@]} 会报 "unbound variable"（bash 4.4 才修好）。
    # 用换行分隔的字符串 + here-string 读取，效果一样且没有这个坑。
    local cands="" c
    for c in /Library/Frameworks/Python.framework/Versions/*/bin/python3.*; do
        [ -x "$c" ] && cands="$cands$c
"
    done
    cands="$cands/opt/homebrew/bin/python3
/usr/local/bin/python3"
    if command -v python3 >/dev/null 2>&1; then
        cands="$cands$(command -v python3)
"
    fi
    cands="$cands/usr/bin/python3"

    # 用 here-string（不是管道）读取：管道会开子 shell，return 就失效了
    while IFS= read -r c; do
        [ -n "$c" ] || continue
        [ -x "$c" ] || continue
        case "$c" in
            /usr/bin/python3)
                # Xcode 命令行工具没装的话，这行会弹窗卡住，直接跳过
                xcode-select -p >/dev/null 2>&1 || continue
                ;;
        esac
        if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' \
                >/dev/null 2>&1; then
            printf '%s\n' "$c"
            return 0
        fi
    done <<< "$cands"
    return 1
}

# ---------------------------------------------------------------------------
# 每个 .command 的公共启动步骤
#   成功后把解释器路径写进全局变量 RADAR_PY
# ---------------------------------------------------------------------------
radar_bootstrap() {
    cd "$RADAR_ROOT" || return 1
    # 统一走 UTF-8（macOS 本来就是，写上是为了和 Windows 端行为一致，
    # 也方便以后有人把项目搬到 Linux 上）
    export PYTHONUTF8=1
    RADAR_PY="$(radar_find_python)" || { RADAR_PY=""; return 1; }
    # 让程序能读到 api_key.txt —— 与 Windows 的 .bat 行为保持一致。
    # 为什么还要导出环境变量：程序里 key 的读取顺序是「环境变量优先，
    # 其次 api_key.txt」，两边都给了才不会有"双击能跑、命令行不能跑"的怪现象。
    if [ -f "$RADAR_ROOT/api_key.txt" ]; then
        DEEPSEEK_API_KEY="$(tr -d '\r\n' < "$RADAR_ROOT/api_key.txt")"
        export DEEPSEEK_API_KEY
    fi
    return 0
}

radar_require_env() {
    if ! radar_bootstrap; then
        radar_err "没有找到 Python 环境（.venv）。"
        radar_info "请先双击「install.command」完成安装，再回来双击这个文件。"
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# launchd：macOS 的定时任务（对应 Windows 的「任务计划程序」）
#   用「在用户登录后由本用户运行」的 LaunchAgent 方式，不需要管理员密码。
# ---------------------------------------------------------------------------
radar_launchd_label() { printf '%s\n' "com.hkjobradar.daily"; }
radar_launchd_plist() { printf '%s\n' "$HOME/Library/LaunchAgents/com.hkjobradar.daily.plist"; }

# XML 转义：路径里万一有 & < > 会写坏 plist
radar_xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

# 生成 plist 内容并输出到 stdout（第二个参数可省略，默认取 RADAR_ROOT）
radar_render_launchd_plist() {
    local py root hour minute
    py="$(radar_xml_escape "${1:?需要 Python 解释器路径}")"
    root="$(radar_xml_escape "${2:-$RADAR_ROOT}")"
    hour="${3:-9}"
    minute="${4:-0}"

    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$(radar_launchd_label)</string>

    <key>ProgramArguments</key>
    <array>
        <string>$py</string>
        <string>$root/run.py</string>
        <string>once</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$root</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUTF8</key>
        <string>1</string>
    </dict>

    <!-- 每天 09:00 跑一轮。电脑当时在睡眠/关机、错过这个点，
         launchd 会在电脑唤醒后补跑一次（这是它自带的行为，不用额外配置）。 -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>$hour</integer>
        <key>Minute</key><integer>$minute</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>$root/data/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$root/data/logs/launchd.err.log</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLIST
}

radar_launchd_install() {
    local py="$1" root="${2:-$RADAR_ROOT}"
    local plist label uid
    plist="$(radar_launchd_plist)"
    label="$(radar_launchd_label)"
    uid="$(id -u)"

    mkdir -p "$(dirname "$plist")" "$root/data/logs" || return 1
    radar_render_launchd_plist "$py" "$root" > "$plist" || return 1

    # 先清掉可能存在的旧任务（不存在会报错，忽略即可）
    launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
    launchctl unload "$plist" >/dev/null 2>&1 || true

    # macOS 10.11+ 用 bootstrap；更老的系统回落到 load
    if ! launchctl bootstrap "gui/$uid" "$plist" >/dev/null 2>&1; then
        launchctl load -w "$plist" >/dev/null 2>&1 || return 1
    fi

    launchctl list 2>/dev/null | grep -q "$label"
}

radar_launchd_uninstall() {
    local plist label uid
    plist="$(radar_launchd_plist)"
    label="$(radar_launchd_label)"
    uid="$(id -u)"

    launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
    launchctl unload "$plist" >/dev/null 2>&1 || true
    [ -f "$plist" ] && rm -f "$plist"
    # 验证：还能在列表里看到就算没删干净
    if launchctl list 2>/dev/null | grep -q "$label"; then
        return 1
    fi
    return 0
}
