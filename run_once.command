#!/bin/bash
# 香港岗位雷达 · 跑一轮就退出（对应 Windows 的 run_once.bat）
# 用途：每天只开电脑一小会儿时用。抓一轮 → 打分 → 推日报 → 自动结束。
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/scripts/common.sh"

radar_require_env || { radar_pause; exit 1; }

echo "正在跑一轮抓取。"
echo "过程中可能会自动打开一个浏览器窗口（JobsDB / LinkedIn / Indeed）。"
echo "每翻一页会随机等 2-8 秒，属于正常现象，请耐心等待。"
echo "结束后日报会推送到你配置的渠道，然后这个窗口会自动停住。"
echo
"$RADAR_PY" "$RADAR_ROOT/run.py" once
echo
echo "---------------------------------------------"
echo "结束。如果上面出现了红色报错，把整个窗口截图发给我。"
echo "---------------------------------------------"
radar_pause
