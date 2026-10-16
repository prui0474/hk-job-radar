#!/bin/bash
# 香港岗位雷达 · 测试推送（对应 Windows 的 notify_test.bat）
# 用途：配好 config.yaml 之后立刻验证一下 —— 收到消息就说明通道没问题。
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/scripts/common.sh"

radar_require_env || { radar_pause; exit 1; }

"$RADAR_PY" "$RADAR_ROOT/run.py" notify test
echo
echo "---------------------------------------------"
echo "收到消息 → 通道配置成功，可以开始用了。"
echo "没收到 → 原因见 data/logs/ 下的日志，"
echo "        也可对照 docs/推送渠道对比.md 的「收不到消息？」一节。"
echo "---------------------------------------------"
radar_pause
