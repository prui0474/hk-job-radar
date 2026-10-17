#!/bin/bash
# 香港岗位雷达 · 登录 LinkedIn（对应 Windows 的 login_linkedin.bat）
# 用途：让抓取时带上你的登录态，能看到的岗位明显更多。
# 建议用一个不常用的小号登录，降低被风控影响主账号的风险。
#
# 两种登录方式，见下面菜单：
#   1 = 打开浏览器窗口手动登录  2 = 从你自己的浏览器搬 cookie
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/scripts/common.sh"

radar_require_env || { radar_pause; exit 1; }

echo "============================================================"
echo " 登录 LinkedIn —— 选一种方式"
echo "============================================================"
echo
echo "  1 = 打开浏览器窗口，在里面登录"
echo "      先试这个。建议用求职专用小号，别用主号。"
echo
echo "  2 = 从你自己的浏览器搬 cookie"
echo "      打开的窗口里验证码一直转、怎么也过不去时，用这个。"
echo "      它不碰验证码：你在日常用的浏览器里正常登录，"
echo "      只把登录凭证复制过来。"
echo
printf "输入 1 或 2 后回车（直接回车 = 1）: "
mode="1"
read -r mode || mode="1"

case "${mode:-1}" in
  2) "$RADAR_PY" "$RADAR_ROOT/run.py" login linkedin --cookie ;;
  *) "$RADAR_PY" "$RADAR_ROOT/run.py" login linkedin ;;
esac
echo
radar_pause
