"""邮件通道的连接方式与报错分类测试：纯逻辑，不联网。

背景（2026-11 真实用户反馈）：填完邮件配置发送报 `Connection unexpectedly closed`，
465 和 587 两个端口各试一遍都失败。根因是 `_connect()` 只看 `use_ssl` 布尔值、
完全没用上端口，而配置模板写着「都能自动识别」，用户按模板填就一定填反。

这里钉住三件事：
1. **端口优先** —— 465 走 SSL、587 走 STARTTLS，其他端口才看 `use_ssl`；
2. **填反不再失败** —— `465 + use_ssl: false` 以前必挂，现在必须能连上；
3. **报错指向正确的修复动作** —— 每种异常都要说出该改哪个字段，
   而不是一律让人去翻垃圾邮件。

同时兼容两种运行方式：
- pytest tests/test_email_connect.py
- python tests/test_email_connect.py
"""
from __future__ import annotations

import socket
import smtplib
import ssl
import sys
from contextlib import contextmanager
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.notify.email_client import EmailChannel


# ---------------------------------------------------------------------------
# 测试替身：假的 smtplib 连接，只记录被调用了什么，不做任何 I/O
# ---------------------------------------------------------------------------
class _FakeConn:
    def __init__(self, host: str, port: int, timeout: int = 30) -> None:
        self.host = host
        self.port = port
        self.starttls_called = False
        self.login_args: tuple[str, str] | None = None
        self.sent: tuple[str, list[str], str] | None = None
        self.closed = False

    def starttls(self) -> None:
        self.starttls_called = True

    def login(self, user: str, auth_code: str) -> None:
        self.login_args = (user, auth_code)

    def sendmail(self, sender: str, to: list[str], body: str) -> None:
        self.sent = (sender, list(to), body)

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc_info) -> bool:
        self.closed = True
        return False


@contextmanager
def _patched_smtp():
    """把 SMTP / SMTP_SSL 换成假实现，记录每次构造用的是哪一个。"""
    calls: list[tuple[str, _FakeConn]] = []
    orig_plain, orig_ssl = smtplib.SMTP, smtplib.SMTP_SSL

    def fake_plain(host, port, timeout=30):
        conn = _FakeConn(host, port, timeout)
        calls.append(("PLAIN", conn))
        return conn

    def fake_ssl(host, port, timeout=30):
        conn = _FakeConn(host, port, timeout)
        calls.append(("SSL", conn))
        return conn

    smtplib.SMTP, smtplib.SMTP_SSL = fake_plain, fake_ssl      # type: ignore[assignment]
    try:
        yield calls
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = orig_plain, orig_ssl  # type: ignore[assignment]


@contextmanager
def _capture_errors():
    """抓 loguru 的 ERROR 日志，用来断言报错文案说了什么。"""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(m.record["message"]), level="ERROR")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def _channel(port: int = 465, use_ssl: bool = True,
             host: str = "smtp.example.com") -> EmailChannel:
    return EmailChannel(host, port, "me@example.com", "authcode",
                        ["to@example.com"], use_ssl)


def _send_with_error(exc: BaseException) -> tuple[bool, str]:
    """让发送过程抛出指定异常，返回 (返回值, 拼起来的 ERROR 日志)。"""
    ch = _channel(port=465)

    def boom(subject: str, html: str) -> None:
        raise exc

    ch._send_mime = boom          # type: ignore[method-assign]  实例属性覆盖，绕开网络
    with _capture_errors() as messages:
        ok = ch.send_html("香港岗位雷达 · 测试", "<p>x</p>")
    return ok, " ".join(messages)


# ---------------------------------------------------------------------------
# 1. 端口决定加密方式
# ---------------------------------------------------------------------------
def test_port_465_uses_implicit_tls():
    """465 是隐式 TLS：一上来就 SMTP_SSL，不做 STARTTLS 升级。"""
    with _patched_smtp() as calls:
        conn = _channel(port=465)._connect()
    assert [kind for kind, _ in calls] == ["SSL"]
    assert conn.starttls_called is False


def test_port_465_ignores_use_ssl_false():
    """★ 用户踩的就是这个：端口 465 却写了 use_ssl: false，以前必挂，现在必须能连。

    端口是确定信息，比一个布尔开关可靠，所以这段行为的语义是「端口优先」。
    """
    with _patched_smtp() as calls:
        conn = _channel(port=465, use_ssl=False)._connect()
    assert [kind for kind, _ in calls] == ["SSL"]
    assert conn.starttls_called is False


def test_port_587_uses_starttls():
    """587 是明文连上再升级，端口对的时候 use_ssl 写什么都无所谓。"""
    with _patched_smtp() as calls:
        conn = _channel(port=587, use_ssl=True)._connect()
    assert [kind for kind, _ in calls] == ["PLAIN"]
    assert conn.starttls_called is True


def test_unknown_port_falls_back_to_use_ssl():
    """自建 SMTP / 企业内网中继用非常规端口时，回落到 use_ssl，不破坏老配置。"""
    with _patched_smtp() as calls:
        _channel(port=2525, use_ssl=True)._connect()
    assert [kind for kind, _ in calls] == ["SSL"]

    with _patched_smtp() as calls:
        conn = _channel(port=2525, use_ssl=False)._connect()
    assert [kind for kind, _ in calls] == ["PLAIN"]
    assert conn.starttls_called is True


def test_mode_label_names_the_actual_method():
    """日志里要说清这次到底怎么连的，否则排查时看不出用的是哪套配置。"""
    assert _channel(port=465, use_ssl=False)._mode_label() == "smtp.example.com:465（SSL）"
    assert _channel(port=587, use_ssl=True)._mode_label() == "smtp.example.com:587（STARTTLS）"
    assert _channel(port=2525, use_ssl=False)._mode_label() == "smtp.example.com:2525（STARTTLS）"


# ---------------------------------------------------------------------------
# 2. 正常发送路径
# ---------------------------------------------------------------------------
def test_send_uses_configured_credentials_and_recipients():
    with _patched_smtp() as calls:
        ok = _channel(port=465).send_html("主题", "<p>hi</p>")

    assert ok is True
    conn = calls[0][1]
    assert conn.login_args == ("me@example.com", "authcode")
    assert conn.sent is not None
    assert conn.sent[1] == ["to@example.com"]
    assert conn.closed is True          # 用完要关，别把连接挂着


# ---------------------------------------------------------------------------
# 3. 报错分类：每种异常都要指向该改的那个字段
# ---------------------------------------------------------------------------
def test_disconnect_points_at_host_and_network():
    """用户实际收到的报错。

    注意：改成端口优先后，465/587 不会再有「填反」这回事，
    所以文案不能再让人去改 use_ssl，否则把用户引到一个不存在的问题上。
    这里同时钉住「不能出现 use_ssl」。
    """
    ok, log = _send_with_error(
        smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))
    assert ok is False
    assert "smtp_host" in log
    assert "465" in log and "587" in log
    assert "use_ssl" not in log


def test_ssl_error_points_at_custom_port_or_proxy():
    ok, log = _send_with_error(
        ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"))
    assert ok is False
    assert "use_ssl" in log
    assert "465" in log


def test_auth_error_points_at_authorisation_code():
    ok, log = _send_with_error(
        smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed"))
    assert ok is False
    assert "授权码" in log


def test_starttls_unsupported_suggests_ssl_port():
    ok, log = _send_with_error(smtplib.SMTPNotSupportedError("STARTTLS not supported"))
    assert ok is False
    assert "465" in log


def test_dns_error_points_at_smtp_host():
    ok, log = _send_with_error(socket.gaierror(11001, "getaddrinfo failed"))
    assert ok is False
    assert "smtp_host" in log


def test_network_error_points_at_firewall():
    ok, log = _send_with_error(TimeoutError("timed out"))
    assert ok is False
    assert "防火墙" in log or "网络" in log


def test_unknown_error_keeps_spam_hint_as_last_resort():
    """兜底分支才提垃圾邮件——连接和登录都排除了，剩下的确实只可能是收件侧。"""
    ok, log = _send_with_error(RuntimeError("something odd"))
    assert ok is False
    assert "垃圾邮件" in log


def test_missing_config_returns_false_without_connecting():
    ch = EmailChannel("smtp.example.com", 465, "", "", [], True)
    with _patched_smtp() as calls:
        assert ch.send_html("主题", "<p>x</p>") is False
    assert calls == []                  # 配置不全时不该发起连接


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
