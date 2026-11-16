"""BrowserManager 页面内请求（fetch_json / open_origin）测试（不联网）。

为什么单独锁这组行为：Workday 的 cxs 接口对 urllib 的请求指纹返回
429 + `Cf-Mitigated: challenge`，而同一个浏览器页面内 fetch 同一接口是 200。
这条链路一旦回归，表现又是「静默 0 条」——所以这里不测真实网络，只测：

1. 正常 JSON 返回给调用方的是 dict；
2. POST 的 payload 原样传进页面脚本；
3. 非 2xx / 非 JSON / fetch 抛错都要抛 BrowserFetchError，带状态码与响应片段；
4. 页面不在目标站点上时必须直接报错（跨域 fetch 会被 CORS 挡）；
5. open_origin 同一主机不重复导航、不同主机各用一张 page、挑战过不去要报错；
6. 换主机时的 goto 被站点自己的重定向打断（interrupted by another navigation）
   时重试一次，且导航后要等 URL 落定（否则 evaluate 撞上 execution context
   destroyed）。

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
OTHER_ORIGIN = "https://other.wd3.myworkdayjobs.com/Experienced-Jobs"


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
        self.wait_calls: list[int] = []
        self.load_state_calls: list[str] = []

    def evaluate(self, script: str, arg=None):          # noqa: ARG002
        self.evaluate_args.append(arg)
        if self._error is not None:
            raise self._error
        return self._result

    def goto(self, url: str, **kwargs):                 # noqa: ARG002
        self.goto_calls.append(url)
        self.url = url
        return None

    def wait_for_timeout(self, ms: int) -> None:
        self.wait_calls.append(ms)

    def wait_for_load_state(self, state: str = "load", timeout: int = 0) -> None:  # noqa: ARG002
        self.load_state_calls.append(state)

    def content(self) -> str:
        return self._content

    def title(self) -> str:
        return "Careers"


class _FakeContext:
    """假 context：只记录 new_page() 次数，返回全新的空白假 page。"""

    def __init__(self) -> None:
        self.new_page_calls = 0
        self.pages: list[_FakePage] = []
        self.closed = False

    def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        page = _FakePage(url="about:blank")
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


def _manager(page: _FakePage, *, source: str = "workday") -> BrowserManager:
    manager = BrowserManager(AppConfig())
    # 预先塞进 _pages / _contexts：launch() 命中就直接返回，不会去起 playwright
    manager._pages[source] = page
    manager._contexts[source] = _FakeContext()
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


def test_fetch_json_retries_after_site_redirect_destroys_context():
    """回归：Manulife / 中信银行国际在 goto 之后 5 秒才自己跳到 /zh-CN/…。

    那一刻页面里的 JS 上下文被销毁，正在跑的 fetch 直接 reject。等新页面
    load 完重发一次即可——固定等待多长都堵不住这个时序。
    """
    class _RedirectedPage(_FakePage):
        def __init__(self) -> None:
            super().__init__(result=None)
            self.rounds = 0

        def evaluate(self, script: str, arg=None):      # noqa: ARG002
            self.evaluate_args.append(arg)
            self.rounds += 1
            if self.rounds == 1:
                raise RuntimeError(
                    "Page.evaluate: Execution context was destroyed, most likely "
                    "because of a navigation")
            return {"ok": True, "status": 200, "text": '{"total": 5}',
                    "cfMitigated": ""}

    page = _RedirectedPage()
    manager = _manager(page)
    assert manager.fetch_json("workday", URL) == {"total": 5}
    assert page.rounds == 2, "撞上跳转应当重发一次"
    assert page.load_state_calls == ["load"], "重发前要等新页面 load 完"


def test_fetch_json_does_not_retry_other_errors():
    """不是「页面正在跳转」的失败不重发：那只会白等一轮。"""
    page = _FakePage(error=RuntimeError("net::ERR_INTERNET_DISCONNECTED"))
    manager = _manager(page)
    try:
        manager.fetch_json("workday", URL)
    except BrowserFetchError as exc:
        assert "ERR_INTERNET_DISCONNECTED" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("网络错误必须直接报错")
    assert len(page.evaluate_args) == 1 and page.load_state_calls == []


# ---------------------------------------------------------------------------
# open_origin
# ---------------------------------------------------------------------------
def test_open_origin_does_not_navigate_twice_for_same_host():
    page = _FakePage(url="about:blank")
    manager = _manager(page)
    manager.open_origin("workday", ORIGIN)
    manager.open_origin("workday", ORIGIN)      # 同站：复用，不重复导航
    assert page.goto_calls == [ORIGIN]
    assert manager._contexts["workday"].new_page_calls == 0


def test_open_origin_uses_one_page_per_host():
    """回归：5 家雇主域名不同，共用一个 page 会让换家时的 goto 被打断。

    实测报错是级联的——上一家的重定向把下一次 goto 打断，5 家里后 4 家
    全部失败。所以一个主机必须有自己的 page（context/profile 仍只有一个）。
    """
    base = _FakePage(url="about:blank")
    manager = _manager(base)
    ctx = manager._contexts["workday"]

    first = manager.open_origin("workday", ORIGIN)
    other = manager.open_origin("workday", OTHER_ORIGIN)

    assert first is base, "第一家用基础 page（启动时它还在 about:blank）"
    assert other is not base, "换主机必须换 page"
    assert ctx.new_page_calls == 1
    assert base.goto_calls == [ORIGIN]
    assert other.goto_calls == [OTHER_ORIGIN]

    # 再回到第一个主机 / 第二次用第二个主机：都命中缓存，各不干扰
    assert manager.open_origin("workday", ORIGIN) is base
    assert manager.open_origin("workday", OTHER_ORIGIN) is other
    assert ctx.new_page_calls == 1
    assert base.goto_calls == [ORIGIN]
    assert other.goto_calls == [OTHER_ORIGIN]


def test_open_origin_retries_navigation_interrupted_by_redirect():
    """站点自己的重定向把 goto 打断时重试一次就够，不该让整家雇主失败。"""
    class _InterruptedOncePage(_FakePage):
        def __init__(self) -> None:
            super().__init__(url="about:blank")
            self._fail = True

        def goto(self, url, **kwargs):                  # noqa: ARG002
            self.goto_calls.append(url)
            if self._fail:
                self._fail = False
                raise RuntimeError(
                    'Page.goto: Navigation to "https://other.example/x" is '
                    'interrupted by another navigation to "https://example/x"')
            self.url = url

    page = _InterruptedOncePage()
    manager = _manager(page)
    got = manager.open_origin("workday", ORIGIN)

    assert got is page
    assert page.goto_calls == [ORIGIN, ORIGIN], "被打断后应当重试一次"
    assert page.wait_calls, "重试前要先等一下，让上一次跳转走完"


def test_open_origin_gives_up_after_second_interruption():
    class _AlwaysInterruptedPage(_FakePage):
        def goto(self, url, **kwargs):                  # noqa: ARG002
            self.goto_calls.append(url)
            raise RuntimeError("Navigation is interrupted by another navigation")

    page = _AlwaysInterruptedPage(url="about:blank")
    manager = _manager(page)
    try:
        manager.open_origin("workday", ORIGIN)
    except BrowserFetchError as exc:
        assert ORIGIN in str(exc) and "interrupted by another navigation" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("重试也失败时必须报错，不能无限重试")
    assert len(page.goto_calls) == 2


def test_wait_url_settled_stops_when_url_repeats():
    """URL 连续两次相同就收手：不能一直转圈等一个不会到的重定向。"""
    class _UrlSequencePage(_FakePage):
        def __init__(self, urls: list[str]) -> None:
            super().__init__(url="")
            self._urls = list(urls)
            self.url_reads = 0

        @property
        def url(self) -> str:                           # type: ignore[override]
            self.url_reads += 1
            # 按顺序取；取完就停在最后一个值上（真实页面不会凭空变回旧 URL）
            return self._urls[min(self.url_reads - 1, len(self._urls) - 1)]

        @url.setter
        def url(self, value: str) -> None:
            pass

    page = _UrlSequencePage(["a", "a", "b", "b"])
    BrowserManager._wait_url_settled(page, tries=20, interval_ms=0)
    assert page.url_reads == 2, f"a/a 就该收手，实际读了 {page.url_reads} 次"
    assert page.wait_calls == [0]

    # 站点自己跳过一次再落定：要停在 b，而不是停在 a
    page = _UrlSequencePage(["a", "b", "b"])
    BrowserManager._wait_url_settled(page, tries=20, interval_ms=0)
    assert page.url_reads == 3


def test_wait_url_settled_gives_up_without_raising():
    """URL 一直在变时到上限就返回，不抛异常——后面的 fetch 自己有超时。"""
    class _ForeverChangingPage(_FakePage):
        def __init__(self) -> None:
            super().__init__(url="")
            self._n = 0

        @property
        def url(self) -> str:                           # type: ignore[override]
            self._n += 1
            return f"https://example/{self._n}"

        @url.setter
        def url(self, value: str) -> None:
            pass

    page = _ForeverChangingPage()
    BrowserManager._wait_url_settled(page, tries=5, interval_ms=0)
    assert page.wait_calls == [0] * 5
    assert page._n == 5


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


def test_close_source_drops_cached_host_pages():
    """同一 source_key 重新 launch 后不能命中已关闭的旧 page。"""
    base = _FakePage(url="about:blank")
    manager = _manager(base)
    manager.open_origin("workday", ORIGIN)
    manager.open_origin("workday", OTHER_ORIGIN)
    assert len(manager._host_pages) == 1

    manager.close_source("workday")
    assert manager._host_pages == {}
    assert "workday" not in manager._pages
    assert "workday" not in manager._contexts


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
