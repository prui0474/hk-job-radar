#!/bin/bash
# 香港岗位雷达 · 立即推送今日日报（对应 Windows 的 report_now.bat）
# 用途：不想等 09:00 定时，现在就想要一份报告。
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/scripts/common.sh"

radar_require_env || { radar_pause; exit 1; }

"$RADAR_PY" "$RADAR_ROOT/run.py" report now
echo
echo "---------------------------------------------"
echo "推送已尝试。如果提示「没有新岗位可推」，"
echo "说明还没抓过岗位 —— 先双击 run_once.command 跑一轮。"
echo "---------------------------------------------"
radar_pause
