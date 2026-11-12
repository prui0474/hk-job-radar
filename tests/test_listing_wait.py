"""列表页等待策略测试：为什么不再等 networkidle。

背景（2026-11 线上事故）：JobsDB 适配器里有一行

    page.wait_for_load_state("networkidle", timeout=30000)

用户和开发者同时撞上 `Timeout 30000ms exceeded`。原因不是网站挂了，是
networkidle 这个信号在招聘站上永远不会到——广告位、埋点、长轮询请求常驻，
「500 毫秒内没有任何网络连接」这个条件不成立。而同一时刻岗位卡片其实
早就渲染好了。

这里锁住三件事：

1. `_wait_for_listing` 等的是**卡片**，等不到就返回 0，**不抛异常**；
2. `_do_fetch` 里**不再出现** networkidle 等待（用会炸的假页面钉死）；
3. 等待超时之后**照样解析**——卡片选择器失配不代表内嵌 JSON 里没数据，
   提前 return 会把本可以拿到的岗位丢掉。

兼容两种运行方式：pytest 与 `python tests/test_listing_wait.py`。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402

from hk_job_radar.config import AppConfig                        # noqa: E402
from hk_job_radar.copilot.form_filler import FormFiller          # noqa: E402
from hk_job_radar.scrapers import jobsdb as jobsdb_module        # noqa: E402
from hk_job_radar.scrapers.jobsdb import SELECTORS, JobsDBScraper  # noqa: E402


# ---------------------------------------------------------------------------
# 假页面 / 假浏览器：只实现适配器会碰到的那几个方法
# ---------------------------------------------------------------------------
class _FakeLocator:
    def __init__(self, count: int, text: str = "") -> None:
        self._count = count
        self._text = text

    def count(self) -> int:
        return self._count

    @property
    def first(self) -> "_FakeLocator":
        return self

    def inner_text(self) -> str:
        return self._text

    def get_attribute(self, name: str):        # noqa: ARG002
        return None

    def all(self) -> list:
        return []


class _FakePage:
    """networkidle 一被调用就炸——把「不许退回去」写进断言里。"""

    def __init__(self, cards: int = 0, *, card_wait_times_out: bool = False,
                 cloudflare_ok: bool = True) -> None:
        self.url = "about:blank"
        self.cards = cards
        self.card_wait_times_out = card_wait_times_out
        self.cloudflare_ok = cloudflare_ok
        self.goto_urls: list[str] = []
        self.selector_waits: list[tuple[str, int | None]] = []

    def goto(self, url: str, **kwargs):        # noqa: ARG002
        self.url = url
        self.goto_urls.append(url)

    def wait_for_load_state(self, state: str, timeout: int | None = None):  # noqa: ARG002
        if state == "networkidle":
            raise AssertionError(
                "不该再等 networkidle：招聘站的长连接让它永不触发，"
                "只会白等 30 秒然后被 tenacity 当成整轮失败")
        raise AssertionError(f"意外的等待状态：{state}")

    def wait_for_selector(self, selector: str, timeout: int | None = None,
                          state: str | None = None):          # noqa: ARG002
        self.selector_waits.append((selector, timeout))
        if self.card_wait_times_out:
            raise PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded.")

    def locator(self, selector: str) -> _FakeLocator:          # noqa: ARG002
        return _FakeLocator(self.cards)


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.delays = 0

    def launch(self, source_key: str, force_headless=None):    # noqa: ARG002
        return self.page

    def wait_cloudflare(self, page, timeout_s=None, quiet=False):  # noqa: ARG002
        return page.cloudflare_ok

    def human_delay(self, min_s=None, max_s=None) -> None:     # noqa: ARG002
        self.delays += 1

    def close_source(self, source_key: str) -> None:           # noqa: ARG002
        pass


def _scraper(page: _FakePage) -> tuple[JobsDBScraper, _FakeBrowser]:
    browser = _FakeBrowser(page)
    scraper = JobsDBScraper(AppConfig(), browser=browser, db=None,   # type: ignore[arg-type]
                            keywords=["data analyst"])
    return scraper, browser


# ---------------------------------------------------------------------------
# 一、异常类型本身：这行 except 必须抓得住
# ---------------------------------------------------------------------------
def test_playwright_timeout_is_not_builtin_timeout():
    """★ Playwright 的 TimeoutError 不继承内置 TimeoutError，也不是 OSError。

    这一条解释了为什么老代码 `except (TimeoutError, OSError)` 抓不住它：
    异常直接穿到 tenacity，被当成偶发故障重试三轮，用户白等三分钟。
    """
    exc = PlaywrightTimeoutError("Timeout 30000ms exceeded.")
    assert not isinstance(exc, TimeoutError)
    assert not isinstance(exc, OSError)
    assert isinstance(exc, Exception)


# ---------------------------------------------------------------------------
# 二、_wait_for_listing 的契约：等卡片，等不到返回 0，绝不抛
# ---------------------------------------------------------------------------
def test_wait_returns_card_count():
    page = _FakePage(cards=7)
    scraper, _ = _scraper(page)
    assert scraper._wait_for_listing(page, "data analyst", 1) == 7
    assert page.selector_waits == [(SELECTORS["job_card"], scraper.CARD_WAIT_MS)]


def test_wait_returns_zero_instead_of_raising_on_timeout():
    """等不到卡片 = 这个关键词没结果，或者页面没渲染出来，两种都不该炸。"""
    page = _FakePage(cards=0, card_wait_times_out=True)
    scraper, _ = _scraper(page)
    assert scraper._wait_for_listing(page, "data analyst", 1) == 0


def test_wait_timeout_is_shorter_than_the_old_thirty_seconds():
    """上限要明显短于 30 秒：超时是常见情况，不能每次都陪满半分钟。"""
    assert JobsDBScraper.CARD_WAIT_MS <= 20000


# ---------------------------------------------------------------------------
# 三、整轮抓取：不再等 networkidle，且超时后照样往下走
# ---------------------------------------------------------------------------
def test_do_fetch_never_waits_for_networkidle():
    """假页面在 networkidle 上会直接 AssertionError，能跑通就说明真没调它。"""
    page = _FakePage(cards=3)
    scraper, browser = _scraper(page)
    parsed: list[str] = []

    def _fake_parse(p, keyword: str):          # noqa: ANN001
        parsed.append(keyword)
        return []

    scraper._parse_listing = _fake_parse      # type: ignore[method-assign]
    scraper._do_fetch(datetime.now())
    assert parsed == ["data analyst"]
    assert len(page.goto_urls) == 1
    assert browser.delays >= 1, "导航后仍要有随机延迟（防封号）"


def test_listing_timeout_does_not_abort_the_page():
    """★ 卡片选择器等超时，仍要调用解析。

    内嵌 JSON（script#__NEXT_DATA__）里的数据比 DOM 早到，卡片选择器失配
    不等于没岗位。在这里提前 return 会把能拿到的岗位丢掉。
    """
    page = _FakePage(cards=0, card_wait_times_out=True)
    scraper, _ = _scraper(page)
    parsed: list[str] = []

    def _fake_parse(p, keyword: str):          # noqa: ANN001
        parsed.append(keyword)
        return []

    scraper._parse_listing = _fake_parse      # type: ignore[method-assign]
    assert scraper._do_fetch(datetime.now()) == []
    assert parsed == ["data analyst"], "等卡片超时后必须继续解析，不能提前收手"


def test_listing_timeout_does_not_trigger_tenacity_retry():
    """超时被吞掉之后，整轮只跑一次——不会出现三轮退避、用户白等三分钟。"""
    page = _FakePage(cards=0, card_wait_times_out=True)
    scraper, _ = _scraper(page)
    scraper._parse_listing = lambda p, kw: []   # type: ignore[method-assign]
    calls = {"n": 0}
    real_do_fetch = scraper._do_fetch

    def _counting(cutoff):                    # noqa: ANN001
        calls["n"] += 1
        return real_do_fetch(cutoff)

    scraper._do_fetch = _counting              # type: ignore[method-assign]
    scraper.fetch_new_jobs(datetime.now())
    assert calls["n"] == 1, "等待超时属于正常情况，不该被 tenacity 重试"


def test_cloudflare_failure_still_needs_manual_verify():
    """人机验证没过的分支不受本次改动影响：仍抛专用异常，仍不重试。"""
    page = _FakePage(cards=0, cloudflare_ok=False)
    scraper, _ = _scraper(page)
    calls = {"n": 0}
    real_do_fetch = scraper._do_fetch          # 先抓住原方法，否则自引用会无限递归

    def _counting(cutoff):                    # noqa: ANN001
        calls["n"] += 1
        return real_do_fetch(cutoff)

    scraper._do_fetch = _counting              # type: ignore[method-assign]
    assert scraper.fetch_new_jobs(datetime.now()) == []
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 四、抓不到东西时要说得出原因
# ---------------------------------------------------------------------------
def _warning_text(scraper: JobsDBScraper) -> str:
    with mock.patch.object(jobsdb_module, "logger") as fake_logger:
        scraper._do_fetch(datetime.now())
    return " ".join(str(c.args[0]) for c in fake_logger.warning.call_args_list)


def test_all_pages_without_cards_explains_what_to_do():
    """★ 只打印「0 个岗位」等于什么都没说。

    「今天真没岗位」和「这台机器抓不到」在日志里长得一模一样，用户唯一
    能做的就是来问一句。所以一页卡片都没等到时，必须把下一步写出来。
    """
    page = _FakePage(cards=0, card_wait_times_out=True)
    scraper, _ = _scraper(page)
    scraper._parse_listing = lambda p, kw: []   # type: ignore[method-assign]
    text = _warning_text(scraper)
    assert "login_jobsdb" in text
    assert "人机验证" in text


def test_cards_present_does_not_emit_the_warning():
    """卡片等到了就是正常空页（关键词没结果），不该吓用户一跳。"""
    page = _FakePage(cards=4)
    scraper, _ = _scraper(page)
    scraper._parse_listing = lambda p, kw: []   # type: ignore[method-assign]
    assert _warning_text(scraper) == ""


# ---------------------------------------------------------------------------
# 五、投递副驾的申请页：同一处写法，同一个坑
# ---------------------------------------------------------------------------
class _FakeApplyPage:
    url = "https://hk.jobsdb.com/job/95074242/apply"
    cloudflare_ok = True

    def __init__(self, controls: int = 0, *, times_out: bool = False) -> None:
        self.controls = controls
        self.times_out = times_out
        self.selector_waits: list[tuple[str, int | None]] = []

    def goto(self, url, **kwargs):            # noqa: ARG002
        pass

    def wait_for_load_state(self, state, timeout=None):  # noqa: ARG002
        if state == "networkidle":
            raise AssertionError("申请页也不该再等 networkidle")
        raise AssertionError(f"意外的等待状态：{state}")

    def wait_for_selector(self, selector, timeout=None, state=None):  # noqa: ARG002
        self.selector_waits.append((selector, timeout))
        if self.times_out:
            raise PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded.")

    def locator(self, selector):              # noqa: ARG002
        return _FakeLocator(self.controls)


def _filler(page: _FakeApplyPage):
    filler = FormFiller.__new__(FormFiller)   # 跳过 __init__：这里只测等待
    filler.browser = _FakeBrowser(page)
    return filler


def test_apply_page_returns_control_count():
    page = _FakeApplyPage(controls=9)
    filler = _filler(page)
    assert filler._wait_for_apply_form(page) == 9
    assert page.selector_waits == [(filler.FORM_READY_SELECTOR, filler.FORM_WAIT_MS)]


def test_apply_page_timeout_returns_zero_without_raising():
    page = _FakeApplyPage(times_out=True)
    filler = _filler(page)
    assert filler._wait_for_apply_form(page) == 0


def test_fill_does_not_wait_for_networkidle_and_still_fills():
    """★ 走完整条 fill()：假页面在 networkidle 上会 AssertionError。"""
    page = _FakeApplyPage(controls=5)
    filler = _filler(page)
    calls: list[str] = []
    filler._wait_for_apply_form = lambda p: calls.append("wait") or 5
    filler._fill_standard_fields = lambda p, r: calls.append("fill")
    filler._upload_resume = lambda p, j, r: None
    filler._screenshot = lambda p: ""

    job = SimpleNamespace(url="https://hk.jobsdb.com/job/95074242/apply")
    result = filler.fill(job)
    assert calls == ["wait", "fill"]
    assert result.platform == "jobsdb"


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
