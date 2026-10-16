#!/bin/bash
# 香港岗位雷达 · 首次登录 JobsDB（对应 Windows 的 login_jobsdb.bat）
# 用途：第一次使用、或以后被网站的安全检查拦下时，跑一次这个。
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/scripts/common.sh"

radar_require_env || { radar_pause; exit 1; }

echo "会打开一个浏览器窗口。"
echo "如果看到了安全验证（Cloudflare 的「正在验证您是否是真人」），"
echo "请手动点一下页面上那个复选框；验证通过后回到这个窗口按回车。"
echo
"$RADAR_PY" "$RADAR_ROOT/run.py" login jobsdb
echo
radar_pause
