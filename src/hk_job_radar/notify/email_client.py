"""邮件通道（email）——SMTP 直发 HTML 岗位卡片。

为什么值得做这个通道：
- **零新增依赖**：smtplib + email 都是 Python 标准库，用户不用多装任何东西；
- **最通用**：只要有邮箱就能收，不受任何第三方平台政策影响；
- **观感最好**：其它通道只能发纯文本，邮件可以发**排好版的 HTML 卡片**
  （岗位名 + 匹配分 + 理由 + 一键投递按钮），在手机上和一个小程序界面几乎无异。

配置（config.yaml）：
    notify:
      channels: [email]
      email:
        smtp_host: "smtp.qq.com"
        smtp_port: 465
        user: "you@qq.com"
        auth_code: "xxxxxxxxxxxxxxxx"     # ← 邮箱的「授权码」，不是登录密码
        to:
          - "you@qq.com"
        use_ssl: true

⚠️ 两个新手必踩的坑（文档与提示里都要讲）：
1. auth_code 是邮箱后台单独生成的**授权码**，不是你的登录密码。腾讯/网易都是这样。
2. 邮件可能进垃圾箱——第一次发送后请去「垃圾邮件」里找一下，标记为「非垃圾邮件」，
   以后就会正常进收件箱。
"""
from __future__ import annotations

import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from loguru import logger

from hk_job_radar.notify.base import NotifyChannel


class EmailChannel(NotifyChannel):
    """SMTP 邮件通道（HTML 卡片）。"""

    name = "email"
    label = "邮件"
    render_mode = "html"          # 收整封 HTML，而不是纯文本分页
    supports_keyboard = False
    supports_feedback = False
    max_chars = 0                 # 邮件没有长度限制

    def __init__(self, smtp_host: str, smtp_port: int, user: str,
                 auth_code: str, to: list[str] | None = None,
                 use_ssl: bool = True) -> None:
        self.smtp_host = (smtp_host or "").strip()
        self.smtp_port = int(smtp_port or 465)
        self.user = (user or "").strip()
        self.auth_code = (auth_code or "").strip()
        self.to = [t for t in (to or []) if str(t).strip()]
        self.use_ssl = bool(use_ssl)

    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.user and self.auth_code and self.to)

    def check(self) -> tuple[bool, str]:
        missing = []
        if not self.smtp_host:
            missing.append("smtp_host")
        if not self.user:
            missing.append("user")
        if not self.auth_code:
            missing.append("auth_code")
        if not self.to:
            missing.append("to")
        if missing:
            return False, "邮件通道缺少：" + "、".join(f"notify.email.{m}" for m in missing)
        return True, "已配置"

    # ------------------------------------------------------------------
    def _connect(self) -> smtplib.SMTP:
        """按端口自动选择 SSL 或 STARTTLS（465 用 SSL，587 用 STARTTLS）。"""
        if self.use_ssl:
            return smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=30)
        conn = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30)
        conn.starttls()
        return conn

    def _send_mime(self, subject: str, html: str) -> None:
        msg = MIMEMultipart("alternative")
        # 主题用 Header 包装：标题里有中文/日期，不包装会变乱码
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = self.user
        msg["To"] = ", ".join(self.to)
        msg.attach(MIMEText(html, "html", "utf-8"))
        with self._connect() as conn:
            conn.login(self.user, self.auth_code)
            conn.sendmail(self.user, self.to, msg.as_string())

    # ------------------------------------------------------------------
    def send_html(self, subject: str, html: str) -> bool:
        """发送 HTML 邮件（日报专用）。"""
        if not self.is_configured():
            logger.error("邮件通道未配置完整（需要 smtp_host / user / auth_code / to），跳过发送。")
            return False
        try:
            self._send_mime(subject, html)
            logger.info(f"邮件已发送 → {'、'.join(self.to)}（主题：{subject}）")
            return True
        except smtplib.SMTPAuthenticationError as exc:
            # 最常见的原因就是用了登录密码而不是授权码，单独给一句人话
            logger.error(f"邮件登录失败：{exc}。请确认填的是邮箱【授权码】而不是登录密码。")
            return False
        except Exception as exc:
            logger.error(f"邮件发送失败：{exc}（若收不到，请检查是否被归入垃圾邮件）")
            return False

    def send_text(self, text: str, keyboard: list | None = None) -> bool:
        """纯文本发送（实时卡片走这里时，包一层极简 HTML 保留换行）。"""
        safe = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\n", "<br>"))
        return self.send_html("香港岗位雷达 · 新岗位提醒",
                              f'<div style="font-family:sans-serif;font-size:14px;'
                              f'line-height:1.7">{safe}</div>')
