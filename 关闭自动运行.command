#!/bin/bash
# 香港岗位雷达 · 关闭「每天 09:00 自动运行」
# 用途：不想让它每天自动跑，就双击这个。随时可以重跑 install.command 再打开。
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/scripts/common.sh"

echo "正在关闭每天 09:00 的自动运行..."
if radar_launchd_uninstall; then
    radar_ok "已关闭。程序本身没受影响，双击 run_once.command 随时可以手动跑。"
else
    radar_warn "没能完全关闭。请把整个窗口截图发给我。"
fi
radar_pause
