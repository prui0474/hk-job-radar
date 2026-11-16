"""Workday 雇主门户适配器：单元测试（不联网）。

为什么值得单独写一组测试：这个适配器走的是**没有官方文档的私有 JSON 接口**，
三处逻辑全部是实测踩出来的（见 workday.py 文件头），而这三处一旦回归，
表现都是「静默零结果」——不报错、不崩溃，只是某天开始一条岗位都抓不到：

- 筛选参数名写成死值（比如 `locationCountry`） → 部分站点直接 400，
  那家公司整家消失；
- 翻页收尾用 `(页数+1)*20 >= total` 判断 → 第 2 页起 total 变 0，
  永远只拿到第一页 20 条；
- `canApply` 不判 → 已关闭岗位照样推给用户，正是这个源本来要解决的问题。

所以下面每一条「回归测试」都对着一个真实发生过的故障写。
"""
from __future__ import annotations

import io
import sys
import tempfile
import urllib.error
from datetime import timedelta
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.config import AppConfig
from hk_job_radar.models import HK_TZ, JobPosting, now_hk
from hk_job_radar.scoring.keyword_filter import KeywordFilter
from hk_job_radar.scrapers import workday as wd
from hk_job_radar.scrapers.workday import WorkdayScraper


# ---------------------------------------------------------------------------
# 造数据的小工具
# ---------------------------------------------------------------------------
EMP = {
    "name": "测试银行",
    "base_url": "https://example.wd3.myworkdayjobs.com",
    "tenant": "example",
    "site": "External",
}


def _facet_payload(*, inner_param: str = "primaryLocation",
                   labels: list[str] | None = None,
                   outer_param: str = "locationMainGroup") -> dict:
    """造一份和真实返回同构的 facets（外层 group 套内层 values）。"""
    labels = labels if labels is not None else ["Hong Kong", "Singapore"]
    return {
        "total": 82,
        "jobPostings": [],
        "facets": [
            {"facetParameter": "timeType", "descriptor": "Time Type",
             "values": [{"descriptor": "Full time", "id": "t1", "count": 82}]},
            {"facetParameter": outer_param, "descriptor": None,
             "values": [{"facetParameter": inner_param,
                         "descriptor": "Primary Location",
                         "values": [{"descriptor": lab, "id": f"id-{lab}", "count": 79}
                                    for lab in labels]}]},
        ],
    }


def _listing_page(paths: list[str], total: int, *, loc: str = "Hong Kong") -> dict:
    return {
        "total": total,
        "jobPostings": [{"title": f"Job {p}", "externalPath": p,
                         "locationsText": loc, "postedOn": "Posted Today"}
                        for p in paths],
    }


class _StubScraper(WorkdayScraper):
    """把两次网络调用换成脚本化返回，其余逻辑全用真的。"""

    def __init__(self, config, pages: list[dict], details: dict[str, dict],
                 facets: dict | None = None):
        # browser 传 None：本适配器不用浏览器
        super().__init__(config, browser=None, db=None)
        self._pages = list(pages)
        self._details = details
        self._facets = facets if facets is not None else _facet_payload()
        self.searches: list[dict] = []

    def _search(self, employer, applied_facets, offset=0, keyword=""):
        self.searches.append({"facets": applied_facets, "offset": offset})
        if not applied_facets and offset == 0 and not self.searches[:-1]:
            return self._facets           # 第一次调用 = 探路拿 facets
        if not self._pages:
            return {"total": 0, "jobPostings": []}
        return self._pages.pop(0)

    def _detail(self, employer, external_path):
        return self._details.get(external_path)


def _cfg() -> AppConfig:
    return AppConfig(keywords=["data analyst", "data scientist"],
                     graduate_keywords=["management trainee"])


def _detail_payload(**over) -> dict:
    base = {
        "title": "Data Analyst",
        "location": "Hong Kong",
        "jobDescription": "<p>We need a <b>data analyst</b>.</p><ul><li>SQL</li></ul>",
        "canApply": True,
        "startDate": "2026-10-18",
        "postedOn": "Posted 2 Days Ago",
        "jobReqId": "JR100",
        "externalUrl": "https://example.wd3.myworkdayjobs.com/job/HK/JR100",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1. HTML → 纯文本
# ---------------------------------------------------------------------------
def test_strip_html_turns_tags_into_plain_text():
    text = wd.strip_html("<p>Hello&nbsp;<b>world</b></p><ul><li>A</li><li>B</li></ul>")
    assert "<" not in text and ">" not in text
    assert "Hello" in text and "world" in text and "A" in text and "B" in text


def test_strip_html_empty_is_safe():
    assert wd.strip_html("") == ""
    assert wd.strip_html(None) == ""


# ---------------------------------------------------------------------------
# 2. 发布时间文案解析（实测只有四种形态）
# ---------------------------------------------------------------------------
def test_posted_on_relative_text():
    sc = WorkdayScraper(_cfg(), browser=None)
    now = now_hk()
    today = sc._extract_posted_at("Posted Today")
    assert today is not None and abs((now - today).total_seconds()) < 120

    yday = sc._extract_posted_at("Posted Yesterday")
    assert yday is not None and 20 < (now - yday).total_seconds() / 3600 < 28

    d6 = sc._extract_posted_at("Posted 6 Days Ago")
    # 比「days == 6」宽松一点点：_extract_posted_at 内部自己取了一次 now，
    # 与上面那行 now 差几微秒就可能让 .days 掉到 5，是本来就会偶发失败的写法。
    assert d6 is not None and abs((now - d6).total_seconds() - 6 * 86400) < 5

    # 「30+ Days Ago」是最后一档，只能当下限处理，不能当成恰好 30 天
    d30 = sc._extract_posted_at("Posted 30+ Days Ago")
    assert d30 is not None and (now - d30).total_seconds() >= 30 * 86400 - 5


def test_posted_on_unknown_text_returns_none():
    """文案变了就返回 None，而不是抛异常把整轮抓取带崩。"""
    sc = WorkdayScraper(_cfg(), browser=None)
    assert sc._extract_posted_at("发布于 3 天前") is None
    assert sc._extract_posted_at("") is None
    assert sc._extract_posted_at(None) is None


def test_start_date_preferred_over_relative_text():
    sc = WorkdayScraper(_cfg(), browser=None)
    assert sc._parse_date_field("2026-10-18").day == 18
    assert sc._parse_date_field("2026-10-18").tzinfo is HK_TZ
    assert sc._parse_date_field("not-a-date") is None
    assert sc._parse_date_field(None) is None


# ---------------------------------------------------------------------------
# 3. 香港判定：严格版 vs 宽容版
# ---------------------------------------------------------------------------
def test_hk_marker_is_strict_but_location_lookup_is_lenient():
    assert WorkdayScraper._has_hk_marker("Hong Kong")
    assert WorkdayScraper._has_hk_marker("Hunghom, Kowloon, Hong Kong")
    assert not WorkdayScraper._has_hk_marker("Singapore")
    # 空描述在严格版里必须为 False：否则「哪个 facet 值是香港」会认错，
    # 把整个筛选参数带歪（实测直接 400）
    assert not WorkdayScraper._has_hk_marker("")

    # 宽容版：多地点 / 空地点放行，宁可多取一次详情也不漏岗位
    assert WorkdayScraper._looks_hong_kong("2 Locations")
    assert WorkdayScraper._looks_hong_kong("")
    assert WorkdayScraper._looks_hong_kong("Sydney") is False


def test_bare_hk_is_not_treated_as_hong_kong():
    """裸 'hk' 会误伤 Tashkent 这类地名，不许当香港标记。"""
    assert not WorkdayScraper._has_hk_marker("Tashkent, Uzbekistan")


# ---------------------------------------------------------------------------
# 4. 筛选参数名必须取「内层」，且香港的几个 id 全要
# ---------------------------------------------------------------------------
def test_discover_hk_facets_uses_inner_facet_parameter():
    """回归：写死 locationCountry 会让部分站点整家 400，必须读内层参数名。"""
    for inner in ("primaryLocation", "locations", "locationCountry",
                  "locationHierarchy2"):
        sc = _StubScraper(_cfg(), [], {}, facets=_facet_payload(inner_param=inner))
        found = sc._discover_hk_facets(EMP)
        assert found is not None, inner
        assert found[0] == inner, f"应该用内层参数名 {inner}，实际 {found[0]}"


def test_discover_hk_facets_collects_all_hk_ids():
    """同一个「Hong Kong」常被拆成两条（市区 / 新界），只取第一条会漏岗位。"""
    sc = _StubScraper(_cfg(), [], {},
                      facets=_facet_payload(labels=["Hong Kong", "Hong Kong",
                                                    "Singapore"]))
    found = sc._discover_hk_facets(EMP)
    assert found is not None
    assert len(found[1]) == 2 and "id-Singapore" not in found[1]


def test_discover_hk_facets_returns_none_when_absent():
    sc = _StubScraper(_cfg(), [], {}, facets=_facet_payload(labels=["Singapore"]))
    assert sc._discover_hk_facets(EMP) is None


# ---------------------------------------------------------------------------
# 5. 翻页：第 2 页 total 变 0 时不许提前收手
# ---------------------------------------------------------------------------
def test_pagination_ignores_zero_total_on_later_pages():
    """回归：曾用 (页数+1)*20 >= total 收尾，第 2 页 total=0 → 永远只拿 20 条。"""
    pages = [
        _listing_page([f"/job/{i}" for i in range(20)], total=54),
        _listing_page([f"/job/{i}" for i in range(20, 40)], total=0),   # 站点怪癖
        _listing_page([f"/job/{i}" for i in range(40, 54)], total=0),
    ]
    sc = _StubScraper(_cfg(), pages, {})
    got = sc._collect_postings(EMP, ("primaryLocation", ["id-Hong Kong"]))
    assert len(got) == 54, f"应该拿到全部 54 条，实际 {len(got)}"


def test_pagination_stops_when_site_wraps_around_to_first_page():
    """翻到底后有些站点会回卷到第一页继续返回数据，必须认得出并收手。"""
    first = [f"/job/{i}" for i in range(20)]
    pages = [_listing_page(first, total=74)] + [_listing_page(first, total=0)] * 5
    sc = _StubScraper(_cfg(), pages, {})
    got = sc._collect_postings(EMP, ("primaryLocation", ["id-Hong Kong"]))
    assert len(got) == 20
    assert len(sc.searches) <= 3, "回卷后应当立刻收手，不该白翻十几页"


def test_pagination_uses_discovered_param_name():
    sc = _StubScraper(_cfg(), [_listing_page(["/job/1"], total=1)], {})
    sc._collect_postings(EMP, ("primaryLocation", ["id-Hong Kong"]))
    assert sc.searches[-1]["facets"] == {"primaryLocation": ["id-Hong Kong"]}


# ---------------------------------------------------------------------------
# 6. canApply / verified_open
# ---------------------------------------------------------------------------
def test_closed_job_is_dropped_by_can_apply():
    """这是整个源存在的理由：接口直接说「不能投了」，就不用猜页面文案。"""
    sc = _StubScraper(
        _cfg(),
        [_listing_page(["/job/open", "/job/closed"], total=2)],
        {"/job/open": _detail_payload(canApply=True, jobReqId="A1"),
         "/job/closed": _detail_payload(canApply=False, jobReqId="A2")},
    )
    jobs = sc._fetch_employer("test", EMP, now_hk() - timedelta(days=7))
    assert [j.external_id for j in jobs] == ["test-A1"]


def test_detail_budget_keeps_title_matches_first():
    """候选数超过详情上限时，标题命中关键词的必须先保。

    为什么这条重要：某家保险公司香港岗位有 74 个，上限只取 60 个详情。
    如果单纯「按接口顺序取前 60」，那条唯一对口的 Data Analyst
    可能刚好排在第 61 位，从此每天都被砍掉——用户永远看不到它。
    """
    total = wd.MAX_DETAILS_PER_EMPLOYER + 1
    items = [{"title": f"Officer {i}", "externalPath": f"/job/{i}",
              "locationsText": "Hong Kong", "postedOn": "Posted Today"}
             for i in range(total - 1)]
    # 对口的那条故意放在最后（接口顺序与相关性无关）
    items.append({"title": "Data Analyst", "externalPath": "/job/hit",
                  "locationsText": "Hong Kong", "postedOn": "Posted 20 Days Ago"})
    details = {it["externalPath"]: _detail_payload(title=it["title"],
                                                  jobReqId=it["externalPath"])
               for it in items}
    sc = _StubScraper(_cfg(), [{"total": total, "jobPostings": items}], details)
    jobs = sc._fetch_employer("test", EMP, now_hk() - timedelta(days=7))
    titles = [j.title for j in jobs]
    assert "Data Analyst" in titles, f"标题命中的岗位被预算砍掉了：{len(titles)} 条"
    assert len(jobs) == wd.MAX_DETAILS_PER_EMPLOYER


def test_workday_job_is_marked_verified_open():
    """verified_open=True 才能让它跳过「岗位新鲜度」年龄闸门。"""
    sc = _StubScraper(_cfg(), [_listing_page(["/job/1"], total=1)],
                      {"/job/1": _detail_payload(jobReqId="A1")})
    jobs = sc._fetch_employer("test", EMP, now_hk() - timedelta(days=7))
    assert jobs and jobs[0].verified_open is True
    assert jobs[0].direct_apply is True          # 雇主官网，天然直投
    assert jobs[0].source == "workday"


def test_external_id_is_prefixed_by_employer_key():
    """多个雇主共用 source='workday'，两侧 reqId 完全可能撞号。"""
    sc = _StubScraper(_cfg(), [_listing_page(["/job/1"], total=1)],
                      {"/job/1": _detail_payload(jobReqId="12345")})
    jobs = sc._fetch_employer("manulife", EMP, now_hk() - timedelta(days=7))
    assert jobs[0].external_id == "manulife-12345"


def test_job_far_outside_location_is_dropped():
    """列表页写「2 Locations」放行，详情页若确认不在香港就要剔除。"""
    sc = _StubScraper(_cfg(),
                      [_listing_page(["/job/sg"], total=1, loc="2 Locations")],
                      {"/job/sg": _detail_payload(location="Singapore", jobReqId="B1")})
    assert sc._fetch_employer("test", EMP, now_hk() - timedelta(days=7)) == []


def test_detail_failure_skips_only_that_job():
    """单条详情 404 不该拖垮整家：最多就是这条不推。"""
    sc = _StubScraper(_cfg(),
                      [_listing_page(["/job/ok", "/job/bad"], total=2)],
                      {"/job/ok": _detail_payload(jobReqId="A1")})   # bad 不在表里
    jobs = sc._fetch_employer("test", EMP, now_hk() - timedelta(days=7))
    assert [j.external_id for j in jobs] == ["test-A1"]


# ---------------------------------------------------------------------------
# 7. 雇主清单：内置 + 用户文件「逐条覆盖」
# ---------------------------------------------------------------------------
def test_employers_merge_keeps_builtins_and_allows_disable():
    original = wd.EMPLOYERS_FILE
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "workday_employers.yaml"
        path.write_text(
            "employers:\n"
            "  hlb:\n"
            "    enabled: false\n"
            "  myshop:\n"
            "    name: 我的公司\n"
            "    base_url: https://myshop.wd3.myworkdayjobs.com\n"
            "    tenant: myshop\n"
            "    site: External\n",
            encoding="utf-8")
        try:
            wd.EMPLOYERS_FILE = path
            merged = wd.load_employers()
        finally:
            wd.EMPLOYERS_FILE = original
    # 内置的其余雇主还在（整份替换会把它们悄悄弄丢）
    assert "manulife" in merged and "cncbi" in merged
    # 同名 key 以用户文件为准
    assert merged["hlb"]["enabled"] is False
    assert merged["hlb"]["name"] == "Hong Leong Bank"      # 没覆盖的字段保留
    # 新增的雇主也在
    assert merged["myshop"]["name"] == "我的公司"


def test_missing_employers_file_uses_builtins():
    original = wd.EMPLOYERS_FILE
    try:
        wd.EMPLOYERS_FILE = Path(tempfile.gettempdir()) / "never_exists_xyz.yaml"
        merged = wd.load_employers()
    finally:
        wd.EMPLOYERS_FILE = original
    assert merged and all(v.get("enabled") for v in merged.values())


def test_broken_employers_yaml_falls_back_to_builtins():
    original = wd.EMPLOYERS_FILE
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "workday_employers.yaml"
        path.write_text("employers: [unclosed", encoding="utf-8")
        try:
            wd.EMPLOYERS_FILE = path
            merged = wd.load_employers()
        finally:
            wd.EMPLOYERS_FILE = original
    assert "manulife" in merged


# ---------------------------------------------------------------------------
# 8. 422 = 参数猜错，要给出人能看懂的提示
# ---------------------------------------------------------------------------
def test_http_422_becomes_actionable_message(monkeypatch=None):
    sc = WorkdayScraper(_cfg(), browser=None)

    def _boom(*a, **k):
        raise urllib.error.HTTPError("https://x", 422, "Unprocessable", {}, io.BytesIO(b""))

    import urllib.request
    original = urllib.request.urlopen
    urllib.request.urlopen = _boom
    try:
        try:
            sc._request("https://x", {"limit": 20})
        except ValueError as exc:
            assert "422" in str(exc) and "tenant" in str(exc)
        else:
            raise AssertionError("422 应当转成 ValueError 并说明是参数问题")
    finally:
        urllib.request.urlopen = original


# ---------------------------------------------------------------------------
# 9. 短关键词不许裸子串匹配（"llm" ⊂ "fulfillment"）
# ---------------------------------------------------------------------------
def test_short_keyword_does_not_match_inside_a_word():
    cfg = AppConfig(keywords=["llm", "data analyst"],
                    graduate_keywords=[])
    f = KeywordFilter(cfg)
    assert f._match("we manage retail sales fulfillment") == ""
    assert f._match("build llm pipelines") == "llm"
    assert f._match("senior llm engineer") == "llm"
    assert f._match("llms at scale") == "llm"          # 复数仍应命中


# ---------------------------------------------------------------------------
# 10. 流水线：verified_open 的岗位跳过年龄闸门
# ---------------------------------------------------------------------------
def test_verified_open_job_bypasses_age_filter():
    from hk_job_radar.pipeline import Pipeline

    old = now_hk() - timedelta(days=60)
    fresh = JobPosting(source="workday", external_id="w1", title="Data Scientist",
                       company="C", url="https://x/1", url_normalized="https://x/1",
                       posted_at=old, verified_open=True)
    stale = JobPosting(source="jobsdb", external_id="j1", title="Data Scientist",
                       company="C", url="https://x/2", url_normalized="https://x/2",
                       posted_at=old)
    # 用最小桩对象调用未绑定方法，避免构造整个 Pipeline（它要浏览器和数据库）
    cfg = _cfg()
    cfg.max_job_age_days = 3
    stub = type("S", (), {"config": cfg})()
    kept = Pipeline._filter_by_age(stub, [fresh, stale])
    assert kept == [fresh], "能确认还在招的岗位不该被年龄砍掉"


# ---------------------------------------------------------------------------
# 11. 请求出口：有浏览器走页面内 fetch，没有才退回 urllib
# ---------------------------------------------------------------------------
class _FakeBrowser:
    """记录调用顺序的假 BrowserManager（只实现 workday 用到的两个方法）。"""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.opened: list[tuple[str, str]] = []
        self.fetches: list[dict] = []

    def open_origin(self, source_key: str, url: str):
        self.opened.append((source_key, url))

    def fetch_json(self, source_key: str, url: str, payload: dict | None = None,
                   timeout_ms: int = 30000) -> dict:      # noqa: ARG002
        self.fetches.append({"source": source_key, "url": url, "payload": payload})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_request_uses_browser_and_opens_employer_site():
    """浏览器是唯一出口：先导航到该雇主站内页面，再 fetch 同域接口。"""
    browser = _FakeBrowser([{"total": 1, "jobPostings": []}])
    sc = WorkdayScraper(_cfg(), browser=browser)
    url = "https://example.wd3.myworkdayjobs.com/wday/cxs/example/External/jobs"
    got = sc._request(url, {"limit": 20},
                      origin_url="https://example.wd3.myworkdayjobs.com/External")
    assert got == {"total": 1, "jobPostings": []}
    assert browser.opened == [("workday", "https://example.wd3.myworkdayjobs.com/External")]
    assert browser.fetches[0]["url"] == url
    assert browser.fetches[0]["payload"] == {"limit": 20}


def test_search_and_detail_route_through_one_shared_page():
    """5 家雇主共用一个 source_key（一个 profile），不是一家一个 context。"""
    browser = _FakeBrowser([{"jobPostings": []},
                            {"jobPostingInfo": {"title": "X"}}])
    sc = WorkdayScraper(_cfg(), browser=browser)
    sc._search(EMP, {})
    sc._detail(EMP, "/job/1")
    assert {src for src, _ in browser.opened} == {"workday"}
    assert browser.opened[0][1] == f"{EMP['base_url']}/{EMP['site']}"
    assert browser.fetches[0]["url"].endswith("/jobs")
    assert browser.fetches[1]["url"].endswith("/job/1")


def test_request_propagates_browser_fetch_error():
    """page.evaluate 失败时要把 BrowserFetchError（带状态码/片段）抛给上层。"""
    from hk_job_radar.scrapers.browser import BrowserFetchError

    err = BrowserFetchError("页面内请求失败：HTTP 429", url="https://x",
                            status=429, snippet="error code: 1015",
                            cf_mitigated=True)
    sc = WorkdayScraper(_cfg(), browser=_FakeBrowser([err]))
    try:
        sc._request("https://example.wd3.myworkdayjobs.com/wday/cxs/e/E/jobs")
    except BrowserFetchError as exc:
        assert exc.status == 429 and exc.cf_mitigated is True
        assert exc.is_cloudflare_challenge is True
    else:                                             # pragma: no cover
        raise AssertionError("浏览器请求失败不能被吞掉")


def test_request_422_via_browser_becomes_actionable_value_error():
    from hk_job_radar.scrapers.browser import BrowserFetchError

    sc = WorkdayScraper(_cfg(), browser=_FakeBrowser([
        BrowserFetchError("HTTP 422", url="https://x", status=422)]))
    try:
        sc._request("https://example.wd3.myworkdayjobs.com/wday/cxs/e/E/jobs")
    except ValueError as exc:
        assert "422" in str(exc) and "tenant" in str(exc)
    else:                                             # pragma: no cover
        raise AssertionError("422 应当提示 tenant / site 写错了")


def test_request_falls_back_to_urllib_without_browser():
    """没有浏览器（单测 / 未安装 playwright）时老路必须还能走。"""
    import urllib.request

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"total": 7, "jobPostings": []}'

    captured = {}

    def _fake_urlopen(req, timeout=0):                # noqa: ARG001
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        return _Resp()

    original = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen
    try:
        sc = WorkdayScraper(_cfg(), browser=None)
        got = sc._request("https://example.wd3.myworkdayjobs.com/wday/cxs/e/E/jobs",
                          {"limit": 20})
    finally:
        urllib.request.urlopen = original
    assert got == {"total": 7, "jobPostings": []}
    assert captured["method"] == "POST"


# ---------------------------------------------------------------------------
# 12. 被 Cloudflare 挑战时不许静默返回空
# ---------------------------------------------------------------------------
def _two_employers() -> dict:
    return {
        "a": {"name": "A 银行", "base_url": "https://a.wd3.myworkdayjobs.com",
              "tenant": "a", "site": "Ext", "enabled": True},
        "b": {"name": "B 保险", "base_url": "https://b.wd3.myworkdayjobs.com",
              "tenant": "b", "site": "Ext", "enabled": True},
    }


def test_all_employers_challenged_logs_error(monkeypatch):
    """全灭 = 链路坏了，必须 error 报出来，不能只是 info 一句「0 个岗位」。"""
    from unittest import mock

    from hk_job_radar.scrapers.browser import BrowserFetchError

    monkeypatch.setattr(wd, "load_employers", _two_employers)
    sc = WorkdayScraper(_cfg(), browser=None)

    def _challenged(key, employer, cutoff):            # noqa: ARG001
        raise BrowserFetchError(
            "页面内请求失败：HTTP 429", url=f"{employer['base_url']}/x",
            status=429, snippet="error code: 1015", cf_mitigated=True)

    monkeypatch.setattr(sc, "_fetch_employer", _challenged)
    with mock.patch.object(wd, "logger") as fake_logger:
        assert sc._do_fetch(now_hk()) == []

    msg = " ".join(str(c.args[0]) for c in fake_logger.error.call_args_list)
    assert "被 Cloudflare 挑战" in msg
    assert "0 条" in msg
    assert "https://a.wd3.myworkdayjobs.com/wday/cxs/a/Ext" in msg
    assert "429" in msg


def test_partial_failure_keeps_working_employers(monkeypatch):
    """一家被挑战只跳过那一家，其余雇主的结果照常返回。"""
    from unittest import mock

    from hk_job_radar.scrapers.browser import BrowserFetchError

    monkeypatch.setattr(wd, "load_employers", _two_employers)
    sc = WorkdayScraper(_cfg(), browser=None)
    job = JobPosting(source="workday", external_id="b-1", title="Data Analyst",
                     company="B 保险", url="https://b/1", url_normalized="https://b/1")

    def _fetch(key, employer, cutoff):                 # noqa: ARG001
        if key == "a":
            raise BrowserFetchError("HTTP 429", url="https://a/x", status=429,
                                    cf_mitigated=True)
        return [job]

    monkeypatch.setattr(sc, "_fetch_employer", _fetch)
    with mock.patch.object(wd, "logger") as fake_logger:
        got = sc._do_fetch(now_hk())
    assert got == [job]
    assert fake_logger.error.call_args_list == [], "只失败一家不该报 error"


def test_facet_400_in_browser_mode_retries_without_filter():
    """浏览器模式下 400 也得能退回「不筛选」——否则这家公司整家消失。"""
    from hk_job_radar.scrapers.browser import BrowserFetchError

    class _Scraper(WorkdayScraper):
        def __init__(self):
            super().__init__(_cfg(), browser=_FakeBrowser([]))
            self.attempts: list = []

        def _discover_hk_facets(self, employer):       # noqa: ARG002
            return ("primaryLocation", ["id-Hong Kong"])

        def _collect_postings(self, employer, hk_facets):   # noqa: ARG002
            self.attempts.append(hk_facets)
            if hk_facets:
                raise BrowserFetchError("HTTP 400", status=400)
            return {"/job/1": {"title": "Data Analyst",
                               "locationsText": "Hong Kong",
                               "postedOn": "Posted Today"}}

        def _fetch_details(self, employer, paths):      # noqa: ARG002
            return {"/job/1": _detail_payload(jobReqId="A1")}

    sc = _Scraper()
    jobs = sc._fetch_employer("test", EMP, now_hk() - timedelta(days=7))
    assert [j.external_id for j in jobs] == ["test-A1"]
    assert sc.attempts == [("primaryLocation", ["id-Hong Kong"]), None]


def test_details_are_serial_when_browser_is_used():
    """Playwright 同步接口绑定线程：浏览器模式下详情必须串行，不能进线程池。"""
    import threading

    sc = WorkdayScraper(_cfg(), browser=_FakeBrowser([]))
    calls: list[tuple[str, str]] = []
    sc._detail = lambda emp, p: calls.append((p, threading.current_thread().name))
    sc._fetch_details(EMP, ["/job/1", "/job/2", "/job/3"])
    here = threading.current_thread().name
    assert [p for p, _ in calls] == ["/job/1", "/job/2", "/job/3"]
    assert all(name == here for _, name in calls)


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
