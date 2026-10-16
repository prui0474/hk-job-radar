"""WxPusher 通道（wxpusher）。

定位：**备用开关**。相比企业微信，它少一步注册、扫码即用；代价是第三方服务、
有免费额度上限、消息排版能力弱一些。所以把它做成「随时可切换的备胎」——
企业微信哪天收紧政策，用户改一行配置就能到这里。

配置（config.yaml）：
    notify:
      channels: [wxpusher]
      wxpusher:
        app_token: "AT_xxxxxxxx"
        uids:
          - "UID_xxxxxxxx"

实现要点：
- 一次 POST 完成发送，没有鉴权握手；
- 免费额度有限（按条计），所以日报分页会**合并到尽量少的条数**；
- 用 contentType=1（纯文本）而不是 markdown：各家客户端渲染差异大，
  纯文本最不容易出乱码（岗位卡片本来就是文本排版，损失不大）。
"""
from __future__ import annotations

import time

import requests
from loguru import logger

from hk_job_radar.notify.base import NotifyChannel, split_text

_API_URL = "https://wxpusher.zjiecode.com/api/send/message"
_MAX_CHARS = 3500        # 保守取值：接口对 content 有长度限制，超了会直接拒收
_SUMMARY_MAX = 20        # summary 是通知栏预览，官方建议 20 字以内


class WxPusherChannel(NotifyChannel):
    """WxPusher 消息推送。"""

    name = "wxpusher"
    label = "WxPusher"
    render_mode = "text"
    supports_keyboard = False
    supports_feedback = False
    max_chars = _MAX_CHARS

    def __init__(self, app_token: str, uids: list[str] | None = None) -> None:
        self.app_token = (app_token or "").strip()
        self.uids = [u for u in (uids or []) if str(u).strip()]

    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        return bool(self.app_token) and bool(self.uids)

    def check(self) -> tuple[bool, str]:
        if not self.app_token:
            return False, "未填写 notify.wxpusher.app_token"
        if not self.uids:
            return False, "未填写 notify.wxpusher.uids（关注公众号后拿到的 UID）"
        return True, "已配置"

    # ------------------------------------------------------------------
    def _post_once(self, content: str, summary: str) -> None:
        payload = {
            "appToken": self.app_token,
            "content": content,
            "summary": summary[:_SUMMARY_MAX],
            "contentType": 1,          # 1 = 纯文本
            "uids": self.uids,
        }
        resp = requests.post(_API_URL, json=payload, timeout=20)
        data = resp.json()
        if data.get("code") != 1000:
            raise RuntimeError(f"WxPusher 返回错误：{data.get('code')} {data.get('msg')}")

    def send_text(self, text: str, keyboard: list | None = None) -> bool:
        """发送一条消息（自动分页）。keyboard 被忽略——本通道没有交互按钮。"""
        if not self.is_configured():
            logger.error("WxPusher 通道未配置 app_token / uids，跳过发送。")
            return False
        pages = split_text(text, _MAX_CHARS)
        summary = self._make_summary(text)
        ok = True
        for i, page in enumerate(pages):
            try:
                self._post_once(page, summary)
            except Exception as exc:
                logger.error(f"WxPusher 第 {i + 1}/{len(pages)} 页发送失败：{exc}")
                ok = False
            if i < len(pages) - 1:
                time.sleep(1)
        return ok

    # ------------------------------------------------------------------
    @staticmethod
    def _make_summary(text: str) -> str:
        """从正文里挑一句当通知栏预览（取第一个非空行，去掉装饰符号）。"""
        for line in text.split("\n"):
            clean = line.strip().strip("━ ").strip()
            if clean:
                return clean[:_SUMMARY_MAX]
        return "香港岗位雷达"
