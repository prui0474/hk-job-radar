"""JobsDB 排序参数与翻页 URL 测试。

背景（2026-11 实测）：JobsDB 搜索结果默认**不按发布时间排序**，是「相关性 +
推广位」的混排。同一个关键词第一页 20 张卡片的时间是
    18d, 14d, 17d, 18d, 4d, 17d, 4h, 13d, 17d, 21d, 27d, 25d, 6d, 7d, 27d, ...
27 天的老岗排在第 11 位，而本项目的 max_job_age_days 是 2 —— 不显式排序的话
一页抓回来九成会被新鲜度过滤剔掉。

实测参数矩阵：
    &sort=date            → 被 302 丢掉，无效
    &sort=ListedDate      → 被 302 丢掉，无效
    &sortmode=ListedDate  → 有效，302 后的 SEO 短链里保留（SEEK 系参数名）

这里锁住三件事：
1. 搜索 URL 里带着 sortmode=ListedDate；
2. 翻页 URL 保留 SEO 短链原有的 query（sortmode / worktype），只改 page；
3. 短链没有 query 时产出 `?page=2`，不是 `&page=2`。

兼容两种运行方式：pytest 与 `python tests/test_jobsdb_sort.py`。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hk_job_radar.config import AppConfig                        # noqa: E402
from hk_job_radar.scrapers import jobsdb as jobsdb_module        # noqa: E402
from hk_job_radar.scrapers.jobsdb import (                       # noqa: E402
    SEARCH_URL,
    JobsDBScraper,
    _page_url,
)

SEO_BASE = ("https://hk.jobsdb.com/data-analyst-jobs/in-Hong-Kong-SAR"
            "?sortmode=ListedDate&worktype=243%2C245")


def _query(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query, keep_blank_values=True)


def _search_url(keyword: str = "data analyst", *,
                worktype: str = "243%2C245") -> str:
    wt = f"&worktype={worktype}" if worktype else ""
    return SEARCH_URL.format(kw=quote_plus(keyword),
                             loc=quote_plus("Hong Kong"), wt=wt)


# ---------------------------------------------------------------------------
# 一、搜索 URL：必须显式要「按发布时间排序」
# ---------------------------------------------------------------------------
def test_search_url_asks_for_listed_date_sort():
    """★ 少了这个参数，抓回来的就是相关性命中的老岗位。

    这是本次修复的核心：默认排序下 27 天的岗位排在第 11 位，而项目只认 2 天内。
    """
    assert "sortmode=ListedDate" in _search_url()


def test_search_url_keeps_worktype_and_keyword():
    """加排序参数不能顺手弄坏关键词 / 地点 / 岗位类型过滤。"""
    url = _search_url("data scientist")
    assert "keywords=data+scientist" in url
    assert "location=Hong+Kong" in url
    assert "worktype=243%2C245" in url


def test_search_url_without_worktype_has_no_dangling_ampersand():
    """没有 job_types 时 {wt} 是空串，不能留下 `...ListedDate&` 这种尾巴。"""
    url = _search_url(worktype="")
    assert url.endswith("sortmode=ListedDate")
    assert "&&" not in url
    assert dict(_query(url))["sortmode"] == "ListedDate"


# ---------------------------------------------------------------------------
# 二、翻页 URL：原有 query 必须带着走
# ---------------------------------------------------------------------------
def test_page_url_keeps_sortmode_and_worktype():
    """★ 翻第 2 页时丢了 sortmode，就等于第 2 页退回无序——最难查的一类 bug。

    它只在 MAX_PAGES_PER_KEYWORD > 1 时暴露，而那一天多半是有人在回填数据，
    不会想到去怀疑 URL 拼装。
    """
    url = _page_url(SEO_BASE, 2)
    params = dict(_query(url))
    assert params["sortmode"] == "ListedDate"
    assert params["worktype"] == "243,245", "parse_qsl 会解码 %2C，等价值"
    assert params["page"] == "2"
    assert "worktype=243%2C245" in url, "重新编码后仍是 %2C，不能变成 %252C"


def test_page_url_separates_page_from_existing_query():
    """第 2 页仍要在同一个 SEO 短链上——换成别的主机/路径就会被重定向回第 1 页。"""
    url = _page_url(SEO_BASE, 2)
    parts = urlsplit(url)
    assert parts.netloc == "hk.jobsdb.com"
    assert parts.path == "/data-analyst-jobs/in-Hong-Kong-SAR"


def test_page_url_adds_question_mark_when_base_has_no_query():
    """短链本来没有 query 时产出 ...?page=2，不能是 ...&page=2（那是无效 URL）。"""
    url = _page_url("https://hk.jobsdb.com/data-analyst-jobs/in-Hong-Kong-SAR", 2)
    assert url == ("https://hk.jobsdb.com/data-analyst-jobs/in-Hong-Kong-SAR"
                   "?page=2")


def test_page_url_replaces_existing_page_instead_of_duplicating():
    """★ 短链里本来带 page 时必须替换，不能拼成 ?page=1&page=2。

    两个 page 参数时站点取哪个不确定 —— 可能永远停在第 1 页且不报错。
    """
    url = _page_url("https://hk.jobsdb.com/x?page=1&sortmode=ListedDate", 3)
    assert [k for k, _ in _query(url)].count("page") == 1
    assert dict(_query(url)) == {"sortmode": "ListedDate", "page": "3"}


# ---------------------------------------------------------------------------
# 三、整轮抓取：第 2 页的 URL 真的经过 goto 发出去
# ---------------------------------------------------------------------------
class _FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count

    @property
    def first(self) -> "_FakeLocator":
        return self

    def inner_text(self) -> str:
        return ""

    def get_attribute(self, name: str):        # noqa: ARG002
        return None

    def all(self) -> list:
        return []


class _RedirectingPage:
    """模拟首页 302 到 SEO 短链接：goto 之后 page.url 换成带 query 的短链。"""

    def __init__(self, landing: str) -> None:
        self.url = "about:blank"
        self.landing = landing
        self.goto_urls: list[str] = []

    def goto(self, url: str, **kwargs):        # noqa: ARG002
        self.goto_urls.append(url)
        # 真实情况下查询 URL 会被 302 到 SEO 短链，这里直接模拟跳转结果
        self.url = self.landing

    def wait_for_selector(self, selector: str, timeout: int | None = None,
                          state: str | None = None):          # noqa: ARG002
        return None

    def locator(self, selector: str) -> _FakeLocator:          # noqa: ARG002
        return _FakeLocator(1)                 # 卡片和「下一页」都存在


class _FakeBrowser:
    def __init__(self, page: _RedirectingPage) -> None:
        self.page = page

    def launch(self, source_key: str, force_headless=None):    # noqa: ARG002
        return self.page

    def wait_cloudflare(self, page, timeout_s=None, quiet=False):  # noqa: ARG002
        return True

    def human_delay(self, min_s=None, max_s=None) -> None:      # noqa: ARG002
        pass


def test_second_page_is_fetched_with_sortmode(monkeypatch):
    """翻页逻辑写死在闭包里时，这条只能在整轮抓取里验。

    MAX_PAGES_PER_KEYWORD 现在是 1，所以这里临时改成 2——正是「有人回填数据
    把页数改回 3」的那天会踩到的路径。
    """
    page = _RedirectingPage(SEO_BASE)
    scraper = JobsDBScraper(AppConfig(), browser=_FakeBrowser(page), db=None,  # type: ignore[arg-type]
                            keywords=["data analyst"])
    scraper._parse_listing = lambda p, kw: []   # type: ignore[method-assign]
    monkeypatch.setattr(jobsdb_module, "MAX_PAGES_PER_KEYWORD", 2)

    scraper._do_fetch(datetime.now())

    assert len(page.goto_urls) == 2, "两页都应发出去（下一页链接存在）"
    first, second = page.goto_urls
    assert "sortmode=ListedDate" in first
    # 第 2 页：带上排序与 worktype，page=2，且只有一个 page
    params = dict(_query(second))
    assert params["sortmode"] == "ListedDate"
    assert params["worktype"] == "243,245"
    assert params["page"] == "2"
    assert [k for k, _ in _query(second)].count("page") == 1


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]
    for fn in _fns:
        if fn.__code__.co_argcount:        # 需要 monkeypatch 的跳过独立运行
            print(f"SKIP  {fn.__name__}（需 pytest 的 monkeypatch）")
            continue
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests collected.")
