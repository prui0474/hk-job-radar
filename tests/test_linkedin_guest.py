"""LinkedIn 适配器：走访客接口而不是 SPA 搜索页。

背景（2026-11-12 实测）：LinkedIn 把 /jobs/search 改成了 React Server Components
前端，302 到 /jobs/search-results，class 全变成 i87l55 这类原子化哈希名，
卡片上没有 /jobs/view 链接，连 <time> 都没有。旧的 `div.job-card-container`、
`li.jobs-search-results__list-item`、`div.base-card`、`div.job-search-card`
四代选择器全部 0 命中——页面能打开、岗位文本也在，但解析出 0 条且不报错。

改走公开访客列表接口后，一页 10 条，结构还是 base-card 那套。这些测试盯的是
「别再退回去」，以及「改版了要吵出来，而不是安静返回 0」。

兼容两种运行方式：pytest 与 `python tests/test_linkedin_guest.py`。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hk_job_radar.config import AppConfig                      # noqa: E402
from hk_job_radar.models import HK_TZ                          # noqa: E402
from hk_job_radar.scrapers import linkedin as li_module        # noqa: E402
from hk_job_radar.scrapers.linkedin import (                   # noqa: E402
    GUEST_SEARCH_URL,
    PAGE_SIZE,
    SELECTORS,
    LinkedInScraper,
)


# ---------------------------------------------------------------------------
# 一、入口与分页
# ---------------------------------------------------------------------------
def test_url_is_the_guest_api_not_the_spa():
    assert "/jobs-guest/jobs/api/seeMoreJobPostings/search" in GUEST_SEARCH_URL
    assert "/jobs/search?" not in GUEST_SEARCH_URL, "SPA 搜索页那套已经没法解析了"
    assert "start={start}" in GUEST_SEARCH_URL
    assert "f_TPR=r86400" in GUEST_SEARCH_URL, "只看最近 24 小时，别翻老页"


def test_page_size_matches_the_endpoint():
    """访客接口固定一页 10 条（不是登录版默认的 25）。"""
    assert PAGE_SIZE == 10


def test_old_module_level_search_url_is_gone():
    """留一个没人用的 SEARCH_URL 只会让人以为还有另一条路。"""
    assert not hasattr(li_module, "SEARCH_URL")


# ---------------------------------------------------------------------------
# 二、选择器只认访客那套结构
# ---------------------------------------------------------------------------
def test_selectors_target_the_guest_markup():
    assert "base-search-card__title" in SELECTORS["title"]
    assert "base-card__full-link" in SELECTORS["job_link"]
    assert "job-search-card__location" in SELECTORS["location"]
    assert "time" in SELECTORS["posted"]


def test_selectors_do_not_reference_the_dead_ui():
    """这四代 class 在新前端里一个都不存在了，写回代码里只会让故障重现。"""
    dead = ("job-card-container", "jobs-search-results__list-item",
            "job-card-list__title--link", "artdeco-entity-lockup__subtitle")
    blob = " ".join(SELECTORS.values())
    for token in dead:
        assert token not in blob, f"{token} 已随新版前端消失，不该再有"


def test_card_selector_requires_a_job_link():
    """`li:has(a[href*=/jobs/view/])` 才不会把嵌套 li、广告位当成岗位。"""
    assert ":has(" in SELECTORS["job_card"] and "/jobs/view/" in SELECTORS["job_card"]


# ---------------------------------------------------------------------------
# 三、时间解析：访客片段给的是中文相对时间 + 日期
# ---------------------------------------------------------------------------
def _scraper() -> LinkedInScraper:
    return LinkedInScraper(AppConfig(), browser=None, db=None, keywords=["data analyst"])  # type: ignore[arg-type]


def test_parses_chinese_relative_time():
    got = _scraper()._extract_posted_at("1 小時前")
    assert got is not None and got.tzinfo is not None
    delta = datetime.now(HK_TZ) - got
    assert timedelta(minutes=50) < delta < timedelta(hours=2)


def test_parses_bare_date_from_datetime_attr():
    got = _scraper()._extract_posted_at("2026-11-12")
    assert got is not None and got.date().isoformat() == "2026-11-12"


def test_unknown_time_returns_none_instead_of_dropping_the_job():
    assert _scraper()._extract_posted_at("時間不明") is None


# ---------------------------------------------------------------------------
# 四、一页都没解析到时要说得出原因
# ---------------------------------------------------------------------------
class _FakeLocator:
    def __init__(self, count: int = 0) -> None:
        self._count = count

    def count(self) -> int:
        return self._count

    def all(self) -> list:
        return []

    @property
    def first(self):
        return self


class _FakePage:
    url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    cloudflare_ok = True

    def __init__(self) -> None:
        self.gotos: list[str] = []

    def goto(self, url, **kwargs):           # noqa: ARG002
        self.gotos.append(url)
        self.url = url

    def wait_for_selector(self, selector, timeout=None, state=None):  # noqa: ARG002
        raise RuntimeError("Timeout 15000ms exceeded.")

    def locator(self, selector):             # noqa: ARG002
        return _FakeLocator(0)

    def content(self) -> str:
        return "<html><body>访客片段</body></html>"

    def title(self) -> str:
        return "Jobs"


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.delays = 0

    def launch(self, source_key: str, force_headless=None):  # noqa: ARG002
        return self.page

    def wait_cloudflare(self, page, timeout_s=None, quiet=False):  # noqa: ARG002
        return True

    def human_delay(self, min_s=None, max_s=None) -> None:   # noqa: ARG002
        self.delays += 1

    def inject_cookies(self, source_key, cookies) -> None:   # noqa: ARG002
        pass


def test_zero_cards_emits_an_actionable_warning():
    """只说「0 个岗位」看不出是没岗位还是又改版了，必须把判断路径写出来。"""
    page = _FakePage()
    scraper = LinkedInScraper(AppConfig(), browser=_FakeBrowser(page),  # type: ignore[arg-type]
                              db=None, keywords=["data analyst"])
    with mock.patch.object(li_module, "logger") as fake_logger:
        assert scraper._do_fetch(datetime.now(HK_TZ) - timedelta(days=1)) == []
    text = " ".join(str(c.args[0]) for c in fake_logger.warning.call_args_list)
    assert "改版" in text or "页面结构" in text
    assert "login_linkedin" in text


def test_pagination_offsets_are_ten_apart():
    """访客接口按 start 偏移翻页，步长必须是一页的 10 条。"""
    u0 = GUEST_SEARCH_URL.format(kw="data+analyst", loc="Hong+Kong", start=0, jt="")
    u1 = GUEST_SEARCH_URL.format(kw="data+analyst", loc="Hong+Kong",
                                 start=PAGE_SIZE, jt="")
    assert "start=0" in u0
    assert "start=10" in u1


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
