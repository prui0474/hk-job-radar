"""BrowserManager 页面内请求（fetch_json / open_origin）测试（不联网）。

为什么单独锁这组行为：Workday 的 cxs 接口对 urllib 的请求指纹返回
429 + `Cf-Mitigated: challenge`，而同一个浏览器页面内 fetch 同一接口是 200。
这条链路一旦回归，表现又是「静默 0 条」——所以这里不测真实网络，只测：

1. 正常 JSON 返回给调用方的是 dict；
2. POST 的 payload 原样传进页面脚本；
3. 非 2xx / 非 JSON / fetch 抛错都要抛 BrowserFetchError，带状态码与响应片段；
4. 页面不在目标站点上时必须直接报错（跨域 fetch 会被 CORS 挡）；
5. open_origin 同站不重复导航、异站必须导航、挑战过不去要报错。

兼容 pytest 与 `python tests/test_browser_fetch_json.py`。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hk_job_radar.config import AppConfig                              # noqa: E402
from hk_job_radar.scrapers.browser import (                           # noqa: E402
    BrowserFetchError,
    BrowserManager,
)

URL = "https://example.wd3.myworkdayjobs.com/wday/cxs/example/External/jobs"
ORIGIN = "https://example.wd3.myworkdayjobs.com/External"


class _FakePage:
    """只实现 fetch_json / open_origin 用到的那几个方法。"""

    def __init__(self, url: str = ORIGIN, *, result=None, error=None,
                 content: str = "<html>ok</html>") -> None:
        self.url = url
        self._result = result
        self._error = error
        self._content = content
        self.evaluate_args: list = []
        self.goto_calls: list[str] = []

    def evaluate(self, script: str, arg=None):          # noqa: ARG002
        self.evaluate_args.append(arg)
        if self._error is not None:
            raise self._error
        return self._result

    def goto(self, url: str, **kwargs):                 # noqa: ARG002
        self.goto_calls.append(url)
        self.url = url
        return None

    def content(self) -> str:
        return self._content

    def title(self) -> str:
        return "Careers"


def _manager(page: _FakePage, *, source: str = "workday") -> BrowserManager:
    manager = BrowserManager(AppConfig())
    # 预先塞进 _pages：launch() 命中就直接返回，不会去起 playwright
    manager._pages[source] = page
    return manager


# ---------------------------------------------------------------------------
# fetch_json
# ---------------------------------------------------------------------------
def test_fetch_json_returns_parsed_dict():
    page = _FakePage(result={"ok": True, "status": 200,
                             "text": '{"total": 3, "jobPostings": []}',
                             "cfMitigated": ""})
    manager = _manager(page)
    data = manager.fetch_json("workday", URL)
    assert data == {"total": 3, "jobPostings": []}


def test_fetch_json_get_has_no_payload_and_post_carries_it():
    page = _FakePage(result={"ok": True, "status": 200, "text": "{}",
                             "cfMitigated": ""})
    manager = _manager(page)
    manager.fetch_json("workday", URL)
    manager.fetch_json("workday", URL, {"limit": 20})
    assert page.evaluate_args[0]["payload"] is None
    assert page.evaluate_args[0]["url"] == URL
    assert page.evaluate_args[1]["payload"] == {"limit": 20}


def test_fetch_json_raises_with_status_and_snippet_on_429():
    page = _FakePage(result={"ok": False, "status": 429,
                             "text": "error code: 1015",
                             "cfMitigated": "challenge"})
    manager = _manager(page)
    try:
        manager.fetch_json("workday", URL)
    except BrowserFetchError as exc:
        assert exc.status == 429
        assert exc.cf_mitigated is True
        assert exc.is_cloudflare_challenge is True
        assert "1015" in exc.snippet
        assert "429" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("非 2xx 必须抛 BrowserFetchError")


def test_fetch_json_raises_on_invalid_json():
    page = _FakePage(result={"ok": True, "status": 200,
                             "text": "<html>not json</html>", "cfMitigated": ""})
    manager = _manager(page)
    try:
        manager.fetch_json("workday", URL)
    except BrowserFetchError as exc:
        assert "JSON" in str(exc)
        assert "not json" in exc.snippet
    else:                                              # pragma: no cover
        raise AssertionError("非 JSON 必须抛 BrowserFetchError")


def test_fetch_json_wraps_page_evaluate_error():
    page = _FakePage(error=RuntimeError("Execution context was destroyed"))
    manager = _manager(page)
    try:
        manager.fetch_json("workday", URL)
    except BrowserFetchError as exc:
        assert URL in str(exc)
        assert "Execution context" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("evaluate 抛错必须转成 BrowserFetchError")


def test_fetch_json_refuses_cross_origin_page():
    """跨域 fetch 会被 CORS 挡，必须在发请求前就给出可读的原因。"""
    page = _FakePage(url="https://other.example/page")
    manager = _manager(page)
    try:
        manager.fetch_json("workday", URL)
    except BrowserFetchError as exc:
        assert "CORS" in str(exc) and "open_origin" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("不同源必须直接报错")
    assert page.evaluate_args == [], "不同源时不该真的发请求"


# ---------------------------------------------------------------------------
# open_origin
# ---------------------------------------------------------------------------
def test_open_origin_navigates_once_per_site():
    page = _FakePage(url="about:blank")
    manager = _manager(page)
    manager.open_origin("workday", ORIGIN)
    manager.open_origin("workday", ORIGIN)      # 同站：复用，不重复导航
    assert page.goto_calls == [ORIGIN]

    other = "https://other.wd3.myworkdayjobs.com/External"
    manager.open_origin("workday", other)       # 换了一家雇主：必须导航
    assert page.goto_calls == [ORIGIN, other]


def test_open_origin_raises_when_challenge_never_passes():
    page = _FakePage(url="about:blank",
                     content="<title>Just a moment...</title>")
    # 等待时长设 0：本轮测试要的是「超时后报错」这个分支，不必真等 3 秒
    cfg = AppConfig(cloudflare_wait_seconds=0)
    manager = BrowserManager(cfg)
    manager._pages["workday"] = page
    try:
        manager.open_origin("workday", ORIGIN)
    except BrowserFetchError as exc:
        assert exc.cf_mitigated is True
        assert ORIGIN in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("挑战过不去必须报错，不能让调用方继续硬发请求")


def test_open_origin_wraps_navigation_error():
    class _BrokenPage(_FakePage):
        def goto(self, url, **kwargs):                  # noqa: ARG002
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

    page = _BrokenPage(url="about:blank")
    manager = _manager(page)
    try:
        manager.open_origin("workday", ORIGIN)
    except BrowserFetchError as exc:
        assert ORIGIN in str(exc) and "ERR_NAME_NOT_RESOLVED" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("导航失败必须转成 BrowserFetchError")


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
