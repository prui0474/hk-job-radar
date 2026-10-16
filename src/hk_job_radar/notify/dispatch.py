"""推送分发器（dispatch）——流水线与具体通道之间的唯一中间层。

它做三件事：
1. **按配置装配通道**：config.yaml 的 notify.channels 写了哪几个，就造哪几个对象；
2. **一次推送、多通道并发发**：日报/实时卡片只生成一次，广播给所有启用通道
   （免费版经 tier 裁剪后只剩 1 个，高级版可以全部打开）；
3. **失败隔离**：任何一路发送失败都只记日志，绝不影响其它通道、也绝不让
   主流程崩掉——"企业微信挂了但我还收到了邮件"正是这个设计要保住的体验。

四个通道完全平等：谁被启用，取决于用户把谁写在 notify.channels 的第一位
（免费版只能留一个），系统不预设任何优先级。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from loguru import logger

from hk_job_radar.db import Database
from hk_job_radar.notify.base import NotifyChannel
from hk_job_radar.notify.email_client import EmailChannel
from hk_job_radar.notify.report import (
    build_daily_report,
    build_feedback_keyboard,
    build_html_report,
    build_realtime_card,
)
from hk_job_radar.notify.telegram_client import TelegramClient
from hk_job_radar.notify.wecom_client import WecomChannel
from hk_job_radar.notify.wxpusher_client import WxPusherChannel

if TYPE_CHECKING:
    from hk_job_radar.config import AppConfig
    from hk_job_radar.models import JobPosting


def build_channel(name: str, cfg: "AppConfig", db: Optional[Database] = None
                  ) -> Optional[NotifyChannel]:
    """按通道名构造通道对象；名字不认识时返回 None（调用方记日志跳过）。

    这是「新增一个通道」唯一需要改的地方：写一个实现 NotifyChannel 的新文件，
    再在这里加一行分支。主流程、排版、调度全都不用动。
    """
    nc = cfg.notify
    if name == "telegram":
        return TelegramClient(nc.telegram.bot_token, nc.telegram.chat_id, db=db)
    if name == "wecom":
        return WecomChannel(nc.wecom.webhook)
    if name == "wxpusher":
        return WxPusherChannel(nc.wxpusher.app_token, nc.wxpusher.uids)
    if name == "email":
        return EmailChannel(nc.email.smtp_host, nc.email.smtp_port, nc.email.user,
                            nc.email.auth_code, nc.email.to, nc.email.use_ssl)
    logger.warning(f"未知的推送通道名「{name}」，已忽略（可选：wecom/wxpusher/email/telegram）")
    return None


class Notifier:
    """多通道推送门面：流水线只跟它打交道，不直接碰任何具体通道。"""

    def __init__(self, channels: list[NotifyChannel]) -> None:
        self.channels = channels

    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, cfg: "AppConfig", db: Optional[Database] = None) -> "Notifier":
        """按 config.notify.channels 的顺序装配通道（顺序即用户的选择顺序）。"""
        built: list[NotifyChannel] = []
        for name in (cfg.notify.channels or []):
            channel = build_channel(name, cfg, db)
            if channel is not None:
                built.append(channel)
        if not built:
            # 不抛异常：让统计/只读命令照常可用，真正的门禁在 require_notify()
            logger.warning("没有可用的推送通道——请检查 config.yaml 的 notify 段。")
        return cls(built)

    # ------------------------------------------------------------------
    @property
    def names(self) -> list[str]:
        return [c.name for c in self.channels]

    @property
    def labels(self) -> list[str]:
        return [c.label for c in self.channels]

    @property
    def has_feedback_channel(self) -> bool:
        """是否有通道支持「点击按钮反馈」（决定要不要启动反馈轮询任务）。"""
        return any(c.supports_feedback for c in self.channels)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def send_text(self, text: str, keyboard: list | None = None) -> dict[str, bool]:
        """发一条文本消息给所有通道。返回 {通道名: 是否成功}（便于诊断）。"""
        return {c.name: self._safe(c.name, lambda ch=c: ch.send_text(text, keyboard))
                for c in self.channels}

    def send_report(self, jobs: list["JobPosting"], show_unscored: bool = True
                    ) -> dict[str, bool]:
        """推送日报：文本排版只算一次，HTML 排版只算一次，然后广播。

        为什么在这里统一排版而不是让每个通道自己排：同一批岗位在不同通道上
        的顺序与内容必须一致，用户换通道时不该看到不一样的结果。
        """
        if not self.channels:
            return {}
        text_channels = [c for c in self.channels if c.render_mode == "text"]
        html_channels = [c for c in self.channels if c.render_mode == "html"]
        results: dict[str, bool] = {}

        if text_channels:
            pages, keyboards = build_daily_report(jobs, show_unscored)
            for c in text_channels:
                results[c.name] = self._safe(c.name, lambda ch=c: ch.send_pages(pages, keyboards))

        if html_channels:
            subject, html_body = build_html_report(jobs, show_unscored)
            for c in html_channels:
                results[c.name] = self._safe(c.name, lambda ch=c: ch.send_html(subject, html_body))

        return results

    def send_realtime(self, job: "JobPosting") -> dict[str, bool]:
        """推送单个高分岗位（免费版不会走到这里，见 tier.realtime_push_enabled）。"""
        results: dict[str, bool] = {}
        for c in self.channels:
            if c.render_mode == "html":
                subject, html_body = build_html_report([job], True)
                subject = f"香港岗位雷达 · 高分新岗位：{job.title}"
                results[c.name] = self._safe(c.name, lambda ch=c: ch.send_html(subject, html_body))
            else:
                results[c.name] = self._safe(
                    c.name,
                    lambda ch=c: ch.send_text(build_realtime_card(job),
                                              build_feedback_keyboard(job)))
        return results

    # ------------------------------------------------------------------
    # 反馈闭环（只有支持反馈的通道会真正做事）
    # ------------------------------------------------------------------
    def poll_feedback(self, on_callback: Callable[[str, str], None]) -> None:
        for c in self.channels:
            if not c.supports_feedback:
                continue
            try:
                c.poll_feedback(on_callback)
            except Exception as exc:
                logger.debug(f"[{c.label}] 反馈轮询异常（下轮重试）：{exc}")

    # ------------------------------------------------------------------
    @staticmethod
    def _safe(name: str, fn: Callable[[], object]) -> bool:
        """包一层：通道内部再离谱的异常都不允许冒到流水线上来。"""
        try:
            result = fn()
            return result is not False
        except Exception as exc:
            logger.error(f"推送通道 [{name}] 发送异常（已隔离，不影响其它通道）：{exc}")
            return False
