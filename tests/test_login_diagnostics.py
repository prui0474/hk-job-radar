"""登录失败时的诊断提示：分清「网络到不了」和「浏览器被判定为自动化」。

为什么值得单独钉住：这两种情况的下一步完全相反 ——
一个是改网络通道（TUN / browser.proxy），另一个是换出口 IP 或亲手点复选框。
如果提示说反了，用户会照着错误的方向折腾很久，而且每一步看起来都"做了"。

同时兼容两种运行方式：
- pytest tests/test_login_diagnostics.py
- python tests/test_login_diagnostics.py（项目 venv 直接运行）
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import hk_job_radar.doctor as doctor
from hk_job_radar.cli import _diagnose_login_failure


def _capture(source: str, reachable: bool) -> str:
    """在「能通 / 不能通」两种网络下跑一次诊断，返回它打印的文字。"""
    original = doctor.tcp_probe
    doctor.tcp_probe = lambda host, port=443, timeout=2.5: reachable
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            _diagnose_login_failure(source)
    finally:
        doctor.tcp_probe = original
    return buffer.getvalue()


def test_unreachable_probe_blames_the_network():
    text = _capture("jobsdb", reachable=False)
    assert "网络这一侧" in text
    assert "challenges.cloudflare.com" in text
    # 必须给出可执行的动作，而不是只说「网络不好」
    assert "TUN" in text
    assert "browser.proxy" in text


def test_reachable_probe_blames_the_browser():
    text = _capture("jobsdb", reachable=True)
    assert "网络是通的" in text
    assert "复选框" in text
    # 不能再建议改网络 —— 那会把用户带偏
    assert "browser.proxy" not in text


def test_linkedin_probes_google_not_cloudflare():
    # LinkedIn 的登录验证依赖 Google reCAPTCHA，不是 Cloudflare
    assert "www.google.com" in _capture("linkedin", reachable=False)
    assert "www.google.com" not in _capture("jobsdb", reachable=False)


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
