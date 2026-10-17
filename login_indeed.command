#!/bin/bash
# 香港岗位雷达 · 检查 Indeed（对应 Windows 的 login_indeed.bat）
# Indeed 没有账号要登录，但它同样有安全验证，而且必须用看得见的窗口。
# 所以这个脚本只做一件事：单独打开一次，确认你的网络能过它的验证。
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/scripts/common.sh"

radar_require_env || { radar_pause; exit 1; }

echo "Indeed 没有账号要登录。它有安全验证，而且必须用看得见的浏览器窗口。"
echo "正因为如此，正常抓取走到 Indeed 时也会弹出一个窗口，那不是出错。"
echo "这个脚本只是让你单独确认一次：你的网络能不能过它的验证。"
echo
"$RADAR_PY" "$RADAR_ROOT/run.py" login indeed
echo
radar_pause
