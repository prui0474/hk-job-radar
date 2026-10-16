#!/bin/bash
# 香港岗位雷达 · 启动常驻监控（对应 Windows 的 start.bat）
# 用途：电脑一直开着时用。它会一直跑，每 N 分钟抓一轮、每天 09:00 推日报。
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/scripts/common.sh"

radar_require_env || { radar_pause; exit 1; }

echo "正在启动监控。这个窗口请保持打开。"
echo "关掉窗口 = 停止监控；想主动停止请按 Control + C。"
echo
"$RADAR_PY" "$RADAR_ROOT/run.py" run
echo
radar_pause
