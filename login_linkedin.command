#!/bin/bash
# 香港岗位雷达 · 登录 LinkedIn（对应 Windows 的 login_linkedin.bat）
# 用途：让抓取时带上你的登录态，能看到的岗位明显更多。
# 建议用一个不常用的小号登录，降低被风控影响主账号的风险。
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/scripts/common.sh"

radar_require_env || { radar_pause; exit 1; }

echo "会打开一个浏览器窗口。"
echo "请在里面用你的 LinkedIn 账号登录，登录完成、能看到信息流后，"
echo "回到这个窗口按回车。登录状态会保存在本机的浏览器档案里，之后自动复用。"
echo
"$RADAR_PY" "$RADAR_ROOT/run.py" login linkedin
echo
radar_pause
