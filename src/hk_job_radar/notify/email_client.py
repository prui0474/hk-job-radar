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

⚠️ 三个新手必踩的坑（文档与提示里都要讲）：
1. auth_code 是邮箱后台单独生成的**授权码**，不是你的登录密码。腾讯/网易都是这样。
2. 邮件可能进垃圾箱——第一次发送后请去「垃圾邮件」里找一下，标记为「非垃圾邮件」，
   以后就会正常进收件箱。
3. 端口和加密方式必须配套：465 是一上来就 TLS，587 是先明文再 STARTTLS。
   填反了不会给你一句「配置错了」，只会甩一句 Connection unexpectedly closed。

第 3 条以前是靠用户自己猜的——代码只看 use_ssl 布尔值，端口完全没用上，
而配置模板却写着「都能自动识别」。2026-11 有用户照着填，两个端口各试一遍全失败。
现在改成端口优先（见 _connect），填反也能连上。
"""
from __future__ import annotations

import smtplib
import socket
import ssl
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
    #: 端口决定加密方式。这两个端口的语义由 RFC 8314 和所有主流邮箱固定：
    #: 465 = 一上来就 TLS（隐式 TLS），587 = 先明文再 STARTTLS。
    IMPLICIT_TLS_PORT = 465
    STARTTLS_PORT = 587

    def _use_implicit_tls(self) -> bool:
        """本次连接该不该一上来就 TLS。

        **端口优先**：465/587 的语义是固定的，不该让用户自己判断。
        只有填了其他端口（自建 SMTP、企业内网中继）才回落到 use_ssl。
        """
        if self.smtp_port == self.IMPLICIT_TLS_PORT:
            return True
        if self.smtp_port == self.STARTTLS_PORT:
            return False
        return self.use_ssl

    def _mode_label(self) -> str:
        """给日志和报错用的一句话，说明本次实际是怎么连的。"""
        mode = "SSL" if self._use_implicit_tls() else "STARTTLS"
        return f"{self.smtp_host}:{self.smtp_port}（{mode}）"

    def _connect(self) -> smtplib.SMTP:
        """按端口选连接方式：465 走 SSL，587 走 STARTTLS，其他端口看 use_ssl。

        ★ 为什么是端口优先而不是 use_ssl 优先：
        以前这里只看 use_ssl，端口填 465 但 use_ssl 写成 false 就必然连不上，
        而配置模板承诺「都能自动识别」。用户会以为是授权码或网络问题，
        在完全无关的方向上反复折腾。端口是确定信息，信它比信一个布尔开关靠谱。
        """
        if self._use_implicit_tls():
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
            logger.info(f"邮件已发送 → {'、'.join(self.to)}（{self._mode_label()}，主题：{subject}）")
            return True
        # ── 异常分类的唯一目的：让用户看完这句话就知道下一步改哪个字段 ──
        # 顺序有讲究：smtplib.SMTPException、ssl.SSLError、socket.gaierror
        # 全都是 OSError 的子类，必须排在 OSError 前面，否则永远轮不到它们。
        except smtplib.SMTPAuthenticationError as exc:
            # 最常见的原因就是用了登录密码而不是授权码，单独给一句人话
            logger.error(f"邮件登录失败（{self._mode_label()}）：{exc}。"
                         f"请确认填的是邮箱【授权码】而不是登录密码——"
                         f"QQ/163 都要在邮箱后台单独生成一个，登录密码在这里用不了。")
            return False
        except ssl.SSLError as exc:
            # 注意：端口 465/587 已经按端口自动选加密方式，这两条路上不会出现
            # 「填反」。能走到这里的只有自定义端口（use_ssl 仍在起作用）或 TLS 被拦截。
            logger.error(f"邮件 TLS 握手失败（{self._mode_label()}）：{exc}。"
                         f"465 和 587 的加密方式已按端口自动选，不需要你填 use_ssl；"
                         f"只有自定义端口才看 use_ssl。所以这里出错通常是"
                         f"中间有代理或防火墙在拦截 TLS，先把 smtp_port 改回 465 再试。")
            return False
        except smtplib.SMTPServerDisconnected as exc:
            logger.error(f"邮件服务器在握手阶段直接断开了连接（{self._mode_label()}）：{exc}。"
                         f"465/587 的加密方式现在按端口自动选，所以这多半不是配置填反，"
                         f"更像是地址或网络问题：确认 smtp_host 没写错"
                         f"（QQ: smtp.qq.com / 163: smtp.163.com / Gmail: smtp.gmail.com）；"
                         f"163 邮箱要先在后台开启 SMTP 服务；"
                         f"再确认这个端口没被本地网络屏蔽，或换成 587 试一次。")
            return False
        except smtplib.SMTPNotSupportedError as exc:
            logger.error(f"邮件服务器不支持 STARTTLS（{self._mode_label()}）：{exc}。"
                         f"把 smtp_port 改成 465（那个端口一上来就是 SSL）。")
            return False
        except socket.gaierror as exc:
            logger.error(f"邮件服务器域名解析失败（smtp_host = {self.smtp_host}）：{exc}。"
                         f"地址拼错了，或本机 DNS 不通（开了代理的话先关掉试试）。")
            return False
        except (TimeoutError, OSError) as exc:
            logger.error(f"邮件连接失败（{self._mode_label()}）：{exc}。"
                         f"网络不通，或这个端口被本地网络/防火墙挡住"
                         f"（公司网络常封 25/465/587，换个网络试试）。")
            return False
        except Exception as exc:
            logger.error(f"邮件发送失败（{self._mode_label()}）：{exc}。"
                         f"连不上和登录失败都已经排除，若只是收不到，检查是否被归入垃圾邮件。")
            return False

    def send_text(self, text: str, keyboard: list | None = None) -> bool:
        """纯文本发送（实时卡片走这里时，包一层极简 HTML 保留换行）。"""
        safe = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\n", "<br>"))
        return self.send_html("香港岗位雷达 · 新岗位提醒",
                              f'<div style="font-family:sans-serif;font-size:14px;'
                              f'line-height:1.7">{safe}</div>')
