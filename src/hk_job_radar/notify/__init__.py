"""推送层（notify/）：Telegram 日报 + 反馈按钮轮询。

P0 范围：telegram_client.py（Bot API 直调）+ report.py（日报排版）。
emailer.py（SMTP 备份通道）属 T04/P1，暂不实现，预留模块位。
"""
from hk_job_radar.notify.report import (
    build_daily_report,
    build_realtime_card,
    build_feedback_keyboard,
)
from hk_job_radar.notify.telegram_client import TelegramClient

__all__ = [
    "TelegramClient",
    "build_daily_report",
    "build_realtime_card",
    "build_feedback_keyboard",
]
