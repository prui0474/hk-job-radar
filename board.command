#!/bin/bash
# 香港岗位雷达 · 岗位工作台（对应 Windows 的 board.bat）
# 用途：在浏览器里看岗位、点「已投/约面」、看进度、点「分析」对照简历。
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/scripts/common.sh"

radar_require_env || { radar_pause; exit 1; }

echo "工作台启动后，把窗口里打印的那一行地址复制到浏览器打开。"
echo "用完之后回到这个窗口按 Ctrl+C，或者直接关掉窗口。"
echo
"$RADAR_PY" "$RADAR_ROOT/run.py" board
echo
echo "---------------------------------------------"
echo "工作台已关闭，没有留下任何后台程序。"
echo "---------------------------------------------"
radar_pause
