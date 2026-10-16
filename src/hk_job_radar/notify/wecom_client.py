"""企业微信群机器人通道（wecom）。

为什么把它放在第一位：它是腾讯官方能力，**免费、没有条数上限**，而且消息
直接进「企业微信」App——用户再把个人微信绑上去，就等于用个人微信收岗位推送，
不需要装任何多余的东西。

配置（config.yaml）：
    notify:
      channels: [wecom]
      wecom:
        webhook: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx"

实现要点：
- 群机器人只认一个 webhook 地址，POST 一个 JSON 就完成发送，没有任何鉴权流程；
- ⚠️ 群机器人的单条 markdown 消息上限是 **4096 字节（不是字符！）**，
  一个中文字算 3 字节，所以实际大约 1300 个汉字就会超——必须按字节切分；
- ⚠️ 限流 20 条/分钟，日报分页多时页间要 sleep；
- 群机器人**不支持交互按钮**，所以 ✅/❌ 反馈在企微通道上不可用（用户点链接自己去投）。
"""
from __future__ import annotations

import time

import requests
from loguru import logger

from hk_job_radar.notify.base import NotifyChannel, split_text_bytes

_WEBHOOK_TMPL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
# 4096 是官方上限，留出余量（markdown 壳、换行）取 3800 字节
_MAX_BYTES = 3800


class WecomChannel(NotifyChannel):
    """企业微信群机器人。"""

    name = "wecom"
    label = "企业微信"
    render_mode = "text"
    supports_keyboard = False
    supports_feedback = False
    max_chars = 1200          # 中文按 3 字节算，1200 字 ≈ 3600 字节，安全

    def __init__(self, webhook: str) -> None:
        self.webhook = (webhook or "").strip()

    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        return bool(self.webhook)

    def check(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "未填写 notify.wecom.webhook"
        return True, "已配置"

    # ------------------------------------------------------------------
    def _post_once(self, content: str) -> None:
        """发一页 markdown；errcode 非 0 视为失败（抛异常给上层记日志）。"""
        resp = requests.post(
            self.webhook,
            json={"msgtype": "markdown", "markdown": {"content": content}},
            timeout=20,
        )
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"企业微信返回错误：{data.get('errcode')} {data.get('errmsg')}")

    def send_text(self, text: str, keyboard: list | None = None) -> bool:
        """发送一条消息（自动按字节分页）。keyboard 被忽略——群机器人没有按钮。"""
        if not self.is_configured():
            logger.error("企业微信通道未配置 webhook，跳过发送。")
            return False
        pages = split_text_bytes(text, _MAX_BYTES)
        ok = True
        for i, page in enumerate(pages):
            try:
                self._post_once(page)
            except Exception as exc:
                # 发送失败只记日志，绝不抛出打断流水线（下轮日报兜底）
                logger.error(f"企业微信第 {i + 1}/{len(pages)} 页发送失败：{exc}")
                ok = False
            if i < len(pages) - 1:
                time.sleep(3)     # 官方限流 20 条/分钟，页间留足间隔
        return ok
