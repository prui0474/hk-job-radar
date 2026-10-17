#!/bin/bash
# 香港岗位雷达 · 一键自检（对应 Windows 的 doctor.bat）
# 用途：哪里感觉不对就双击它 —— 15 项体检，逐条告诉你没配好的地方该怎么修。
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/scripts/common.sh"

radar_require_env || { radar_pause; exit 1; }

"$RADAR_PY" "$RADAR_ROOT/run.py" doctor

echo
echo "---------------------------------------------"
echo "标 ❌ 的是必须修的，每一条都写清了怎么改。"
echo "看不懂的地方，把这个窗口截图保存下来即可。"
echo "---------------------------------------------"
radar_pause
