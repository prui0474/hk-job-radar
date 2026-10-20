"""代理探测、验证态预检、旧系统兼容判定的单元测试。

为什么这几个测试重要：
1. doctor 第 5 项曾经用「裸 socket 直连」的结果去判断「配了代理通不通」，
   于是用户填对了代理也照样被报「目标仍然不通」——这条误报会把所有人
   引去查端口，白折腾半天。下面的用例把「走代理」这条路钉死。
2. 「没过人机验证」如果被 tenacity 重试，用户要多等三分钟才看到同一句话。
   测试确保它只执行一次就返回。
3. 旧版 macOS 上 playwright 运行器会崩（dyld / Symbol not found），
   这种情况必须和「没下载」区分开，否则提示会指错方向。

同时兼容两种运行方式：
- pytest tests/test_proxy_and_verify.py
- python tests/test_proxy_and_verify.py
"""
from __future__ import annotations

import socket
import sys
import threading
import types
from datetime import datetime
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import hk_job_radar.doctor as doc
from hk_job_radar.config import AppConfig
from hk_job_radar.scrapers.base import (
    BaseScraper,
    NeedsManualVerifyError,
)


# ---------------------------------------------------------------------------
# 一、代理地址解析
# ---------------------------------------------------------------------------
def test_proxy_endpoint_accepts_common_forms():
    assert doc._proxy_endpoint("http://127.0.0.1:7897") == ("127.0.0.1", 7897)
    # 不带协议头：Cliash / v2rayN 的用户常这么填
    assert doc._proxy_endpoint("127.0.0.1:7897") == ("127.0.0.1", 7897)
    assert doc._proxy_endpoint("socks5://127.0.0.1:7891") == ("127.0.0.1", 7891)
    # socks 不写端口时用 1080，http 用 8080
    assert doc._proxy_endpoint("socks5://10.0.0.2") == ("10.0.0.2", 1080)
    assert doc._proxy_endpoint("http://10.0.0.2") == ("10.0.0.2", 8080)


def test_proxy_endpoint_rejects_empty_and_broken():
    assert doc._proxy_endpoint("") is None
    assert doc._proxy_endpoint("   ") is None
    assert doc._proxy_endpoint("http://") is None
    assert doc._proxy_endpoint("http://127.0.0.1:abcd") is None


# ---------------------------------------------------------------------------
# 二、真的走一次代理协议（本地假服务器，不联网）
# ---------------------------------------------------------------------------
def _closed_port() -> int:
    """拿一个当前没人监听的端口。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _fake_http_proxy(reply: bytes) -> int:
    """起一个只回一次的假 HTTP 代理，返回它的端口。"""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        try:
            conn.recv(1024)
            conn.sendall(reply)
        except OSError:
            pass
        finally:
            conn.close()
            server.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


def _fake_socks5_proxy(rep: int) -> int:
    """起一个假 SOCKS5 代理，按 rep 回包（0=成功）。"""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        try:
            conn.recv(3)                     # greeting
            conn.sendall(b"\x05\x00")        # 无需认证
            conn.recv(256)                   # CONNECT 请求
            conn.sendall(bytes([5, rep, 0, 1]) + b"\x00" * 6)
        except OSError:
            pass
        finally:
            conn.close()
            server.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


def test_http_proxy_reports_reachable_on_200():
    port = _fake_http_proxy(b"HTTP/1.1 200 Connection established\r\n\r\n")
    assert doc.proxy_probe(f"http://127.0.0.1:{port}", "example.com") is True


def test_http_proxy_reports_blocked_on_403():
    port = _fake_http_proxy(b"HTTP/1.1 403 Forbidden\r\n\r\n")
    assert doc.proxy_probe(f"http://127.0.0.1:{port}", "example.com") is False


def test_socks5_proxy_handshake():
    port = _fake_socks5_proxy(0)
    assert doc.proxy_probe(f"socks5://127.0.0.1:{port}", "example.com") is True
    port = _fake_socks5_proxy(5)                   # 5 = 连接被拒
    assert doc.proxy_probe(f"socks5://127.0.0.1:{port}", "example.com") is False


def test_proxy_port_open_is_false_for_dead_port():
    assert doc.proxy_port_open(f"127.0.0.1:{_closed_port()}", timeout=0.6) is False


def test_proxy_probe_returns_none_for_unsupported_scheme():
    # socks4 不支持：返回 None，调用方应退回直连判断而不是报「不通」
    assert doc.proxy_probe("socks4://127.0.0.1:9", "example.com") is None


# ---------------------------------------------------------------------------
# 三、_check_network 必须真的走代理（这是当初那条误报的回归测试）
# ---------------------------------------------------------------------------
def _network_with(proxy_setting: str, *, port_open: bool, via_proxy: bool,
                  direct: bool = False, system_proxy: str = "") -> doc.Check:
    with mock.patch.object(doc, "proxy_port_open", lambda p, timeout=2.0: port_open), \
         mock.patch.object(doc, "proxy_probe",
                           lambda p, h, port=443, timeout=4.0: via_proxy), \
         mock.patch.object(doc, "tcp_probe",
                           lambda h, port=443, timeout=2.5: direct), \
         mock.patch.object(doc, "_system_proxy", lambda: system_proxy):
        return doc._check_network(AppConfig(browser_proxy=proxy_setting))


def test_network_ok_through_proxy_even_when_direct_fails():
    """直连不通、但代理通 —— 以前这里会被误报成「目标仍然不通」。"""
    check = _network_with("http://127.0.0.1:7897", port_open=True,
                          via_proxy=True, direct=False)
    assert check.status == doc.OK
    assert "经" in check.detail


def test_network_says_proxy_port_dead_when_nothing_listens():
    check = _network_with("http://127.0.0.1:7897", port_open=False, via_proxy=False)
    assert check.status == doc.WARN
    assert "代理端口连不上" in check.detail
    assert "7897" in check.hint


def test_network_blames_node_not_config_when_proxy_alive_but_target_blocked():
    check = _network_with("http://127.0.0.1:7897", port_open=True, via_proxy=False)
    assert check.status == doc.WARN
    assert "换一个节点" in check.hint


def test_network_system_mode_without_system_proxy_says_so():
    check = _network_with("system", port_open=False, via_proxy=False, system_proxy="")
    assert check.status == doc.WARN
    assert "没有配系统代理" in check.hint


def test_network_keeps_tun_advice_when_nothing_configured():
    check = _network_with("", port_open=False, via_proxy=False,
                          direct=False, system_proxy="")
    assert check.status == doc.WARN
    assert "TUN" in check.hint


# ---------------------------------------------------------------------------
# 四、没过验证时不能重试（否则用户白等三分钟）
# ---------------------------------------------------------------------------
class _VerifyBlockedScraper(BaseScraper):
    source_name = "jobsdb"

    def __init__(self) -> None:
        super().__init__(AppConfig(), browser=None)   # type: ignore[arg-type]
        self.calls = 0

    def _do_fetch(self, last_seen_cutoff: datetime) -> list:
        self.calls += 1
        raise NeedsManualVerifyError(self.source_name)

    def _extract_posted_at(self, raw: str):          # noqa: ANN001
        return None


def test_needs_manual_verify_is_not_retried():
    scraper = _VerifyBlockedScraper()
    result = scraper.fetch_new_jobs(datetime.now())
    assert result == []
    assert scraper.calls == 1, "没过验证不该重试，否则用户要多等三轮"


def test_needs_manual_verify_message_points_to_double_click():
    """提示里必须出现可双击的文件名，而不是命令行。"""
    scraper = _VerifyBlockedScraper()
    with mock.patch("hk_job_radar.scrapers.base.logger") as fake_logger:
        scraper.fetch_new_jobs(datetime.now())
    printed = " ".join(str(c.args[0]) for c in fake_logger.error.call_args_list)
    assert "login_jobsdb" in printed
    assert "双击" in printed
    assert "python run.py" not in printed


# ---------------------------------------------------------------------------
# 五、旧系统兼容：dyld 报错要和「没下载」区分开
# ---------------------------------------------------------------------------
def test_driver_incompatible_markers_detected():
    for text in (
        "dyld: Symbol not found: __ZNSt3__113basic_filebuf",
        "Referenced from: node (which was built for Mac OS X 13.5)",
        "Library not loaded: /usr/lib/libc++.1.dylib",
    ):
        assert doc._looks_like_driver_incompatible(RuntimeError(text)), text


def test_ordinary_errors_are_not_mistaken_for_driver_issues():
    for text in ("Timeout 30000ms exceeded", "net::ERR_CONNECTION_RESET",
                 "Chromium is not installed"):
        assert not doc._looks_like_driver_incompatible(RuntimeError(text)), text


def test_chromium_check_explains_old_macos_instead_of_network():
    fake_module = types.ModuleType("playwright.sync_api")

    def boom():
        raise RuntimeError(
            "dyld: Symbol not found: __ZNSt3__113basic_filebufIcNS_11char_traits"
            "IcEEE4openEPKcj (built for Mac OS X 13.5)")

    fake_module.sync_playwright = boom
    with mock.patch.dict(sys.modules, {"playwright.sync_api": fake_module}), \
         mock.patch.object(doc, "_macos_version", lambda: (12, 6)):
        check = doc._check_chromium()
    assert check.status == doc.FAIL
    assert "12.6" in check.detail
    assert "13.5" in check.hint
    assert "网络" in check.hint          # 明确否认「这是网络问题」


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
