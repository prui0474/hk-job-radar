#!/bin/bash
# 香港岗位雷达 · 配置向导（对应 Windows 的 setup.bat）
# 用途：在浏览器里点选填配置，不用手改 YAML，也不会改错缩进。
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/scripts/common.sh"

radar_require_env || { radar_pause; exit 1; }

# 让 api_key.txt 里的 key 在向导进程里也可见（体检那一步会用到）
if [ -f "$RADAR_ROOT/api_key.txt" ]; then
  export DEEPSEEK_API_KEY="$(tr -d '\r\n' < "$RADAR_ROOT/api_key.txt")"
fi

"$RADAR_PY" "$RADAR_ROOT/run.py" setup

echo
echo "---------------------------------------------"
echo "向导已关闭。如果配置已经保存，双击 start.command 即可开始。"
echo "想再改配置，随时重新双击本文件。"
echo "---------------------------------------------"
radar_pause
