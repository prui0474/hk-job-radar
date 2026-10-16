"""推送层（notify/）：多通道推送 + 日报排版 + 反馈轮询。

结构（每个通道一个文件，互不干扰）：
    base.py              通道接口（所有通道的"样板"）
    dispatch.py          分发器（按配置装配通道 + 一次性广播 + 失败隔离）
    telegram_client.py   Telegram（唯一支持 ✅/❌ 按钮反馈的通道）
    wecom_client.py      企业微信群机器人（腾讯官方、免费、无条数上限）
    wxpusher_client.py   WxPusher（扫码即用，作为备用开关）
    email_client.py      邮件 SMTP（零新增依赖、可发 HTML 岗位卡片）
    report.py            排版（文本分页 + HTML 卡片，与通道解耦）

四个通道完全平等，用哪个由 config.yaml 的 notify.channels 决定；
免费版经 tier 裁剪后只保留其中一个（详见 hk_job_radar/tier.py）。
"""
from hk_job_radar.notify.base import NotifyChannel
from hk_job_radar.notify.dispatch import Notifier, build_channel
from hk_job_radar.notify.email_client import EmailChannel
from hk_job_radar.notify.report import (
    build_daily_report,
    build_feedback_keyboard,
    build_html_card,
    build_html_report,
    build_realtime_card,
)
from hk_job_radar.notify.telegram_client import TelegramClient
from hk_job_radar.notify.wecom_client import WecomChannel
from hk_job_radar.notify.wxpusher_client import WxPusherChannel

__all__ = [
    "NotifyChannel",
    "Notifier",
    "build_channel",
    "TelegramClient",
    "WecomChannel",
    "WxPusherChannel",
    "EmailChannel",
    "build_daily_report",
    "build_realtime_card",
    "build_feedback_keyboard",
    "build_html_report",
    "build_html_card",
]
