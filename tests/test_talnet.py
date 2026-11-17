"""tal.net 校招站适配器：单元测试（不联网）。

为什么单独写一组测试：这个源的失效方式是「静默少抓」，不报错、不崩溃：

- 香港判定只看标题 → Fidelity 那种「地点在表格列里」的租户整家归零；
- 香港判定只看地点 → Jefferies 的磁贴没有地点字段，4 条投行暑期实习全丢；
- 表格 / 磁贴双模板不去重 → Fidelity 同一条岗位入库两次；
- 分页收尾写成 `页数*50 >= total` → 第 2 页仍是 75 条总数时会把 25 条漏掉
  （或直接用 page 参数，站点不认，永远只拿第一页）。

fixture 是 2026-11-18 从真实 board 剪下来的原始片段（只删了无关岗位），
不是手写的假 HTML——手写的那份永远比线上干净，测不出真问题。
"""
from __future__ import annotations

import sys
import tempfile
import urllib.error
from datetime import timedelta
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.config import AppConfig
from hk_job_radar.models import now_hk
from hk_job_radar.scoring.keyword_filter import KeywordFilter
from hk_job_radar.scrapers import talnet as tn
from hk_job_radar.scrapers.talnet import TalNetScraper

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / f"talnet_{name}.html").read_text(encoding="utf-8")


def _cfg() -> AppConfig:
    return AppConfig(keywords=["data analyst"], graduate_keywords=[])


class _StubScraper(TalNetScraper):
    """把 HTTP 换成脚本化返回，其余逻辑（解析/过滤/拼装）全用真的。"""

    def __init__(self, config: AppConfig | None = None, pages: list | None = None):
        # browser 传 None：本适配器不用浏览器
        super().__init__(config or _cfg(), browser=None, db=None)
        self._pages = list(pages or [])
        self.urls: list[str] = []

    def _http_get(self, url: str) -> str:              # noqa: ARG002
        self.urls.append(url)
        if not self._pages:
            return ""
        item = self._pages.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------------------
# 造页面：只用于「分页 / 数量」这类 fixture 无法覆盖的场景
# ---------------------------------------------------------------------------
def _tile(opp_id: int, title: str) -> str:
    return (
        f'<li class="col-md-6 opp-container" id="oppid-{opp_id}" data-oppid="{opp_id}">'
        f'<div class="opp_{opp_id} search_res details_row candidate-opp-tile" '
        f'data-oppid="{opp_id}" data-title="{title}">'
        f'<h3 class="candidate-opp-field-2">'
        f'<a class="subject" href="/opp/{opp_id}">{title}</a></h3>'
        f'</div></li>')


def _page(count: int, *, first_id: int = 1000, total: int | None = None,
          suffix: str = " - Hong Kong") -> str:
    tiles = "\n".join(_tile(first_id + i, f"Job {first_id + i}{suffix}")
                      for i in range(count))
    return f'<h2 role="alert">{count if total is None else total} results match!</h2>{tiles}'


def _row(opp_id: int, title: str, city: str = "") -> str:
    cells = (f'<td class="comm_list_tbody">'
             f'<a class="subject" href="/opp/{opp_id}">{title}</a></td>')
    if city:
        cells += f'<td class="comm_list_tbody">{city}</td>'
    return (f'<tr class="opp_{opp_id} search_res details_row" data-oppid="{opp_id}" '
            f'data-title="{title}">{cells}</tr>')


# ---------------------------------------------------------------------------
# 1. 解析：表格模板 / 磁贴模板
# ---------------------------------------------------------------------------
def test_row_template_parses_id_title_and_url():
    total, items = tn.parse_board(_fixture("fidelity"))
    assert total == 24
    by_id = {it["opp_id"]: it for it in items}
    hk = by_id["1441"]
    assert hk["title"] == "Equity Research MBA Intern Programme 2027 - Hong Kong"
    assert hk["url"] == (
        "https://fidelityinternational.tal.net/vx/lang-en-GB/mobile-0/brand-5/"
        "candidate/so/pm/1/pl/1/opp/1441-Equity-Research-MBA-Intern-Programme-2027-Hong-Kong/en-GB")


def test_tile_template_parses_id_title_and_url():
    total, items = tn.parse_board(_fixture("jefferies"))
    assert total == 75
    by_id = {it["opp_id"]: it for it in items}
    job = by_id["1997"]
    # data-title 里的 &amp; 要反转义，否则标题会带着实体进日报
    assert job["title"] == ("2027 Equities Sales & Trading Summer Analyst Program "
                            "- Cash Trading - Hong Kong")
    assert job["url"].startswith("https://jefferies.tal.net/")
    assert "/opp/1997-" in job["url"]


def test_row_and_tile_on_same_page_are_deduped_by_opp_id():
    """Fidelity 同一页里表格与磁贴各一份，不去重就会入库两次。"""
    _, items = tn.parse_board(_fixture("fidelity"))
    ids = [it["opp_id"] for it in items]
    assert len(ids) == len(set(ids))
    assert len(ids) == 7, f"6 行 + 1 个磁贴独有项 = 7，实际 {ids}"


def test_tile_id_field_is_not_mistaken_for_location():
    """Jefferies 磁贴的 `ID: 1997` 不是地点，不能进 location。"""
    _, items = tn.parse_board(_fixture("jefferies"))
    assert all(it["location"] == "" for it in items)


def test_empty_html_is_safe():
    assert tn.parse_board("") == (None, [])
    assert tn.parse_board("<html></html>") == (None, [])


# ---------------------------------------------------------------------------
# 2. 香港判定：标题 / 地点各看一处
# ---------------------------------------------------------------------------
def test_hk_from_title_suffix():
    """Jefferies 的卡片没有地点列，只能靠标题里的 "- Hong Kong"。"""
    assert TalNetScraper._is_hong_kong(
        "2027 Information Technology Summer Analyst (Infrastructure) - Hong Kong", "")
    assert not TalNetScraper._is_hong_kong("2027 Asset Management Internship - London", "")


def test_hk_from_location_column_when_title_is_silent():
    """BofA 那类租户：标题一个字不提香港，城市在第 3 列。"""
    assert TalNetScraper._is_hong_kong("Summer Analyst Programme 2027", "Hong Kong")
    assert TalNetScraper._is_hong_kong("Summer Analyst Programme 2027", "HK")


def test_bare_hk_is_not_matched_inside_a_word():
    """裸 'hk' 会命中 Tashkent（T-a-s-hk-ent），必须走词边界。"""
    assert not TalNetScraper._is_hong_kong("Analyst", "Tashkent, Uzbekistan")


def test_job_without_hk_anywhere_is_dropped():
    """标题和地点都没有香港 → 丢弃（不能靠「可能是香港」放行）。"""
    sc = _StubScraper(pages=[_row(1, "Data Analyst", "Singapore")])
    assert sc._fetch_board("x", tn.DEFAULT_BOARDS["jefferies"]) == []


def test_location_only_hk_job_keeps_city_as_location():
    sc = _StubScraper(pages=[_row(1, "Summer Analyst Programme 2027", "Hong Kong")])
    jobs = sc._fetch_board("x", tn.DEFAULT_BOARDS["jefferies"])
    assert len(jobs) == 1
    assert jobs[0].location == "Hong Kong"


# ---------------------------------------------------------------------------
# 3. 翻页：`?start=50`，不足一页即停
# ---------------------------------------------------------------------------
def test_pagination_uses_start_offset():
    sc = _StubScraper(pages=[_page(50, total=53), _page(3, first_id=2000, total=53)])
    sc._collect_board("jefferies", tn.DEFAULT_BOARDS["jefferies"])
    assert sc.urls[0].endswith("/candidate/jobboard/vacancy/2/adv/")
    assert sc.urls[1].endswith("?start=50"), sc.urls


def test_pagination_stops_on_short_page():
    """第 2 页只有 3 条 → 到底了，不该再发第 3 次请求。"""
    sc = _StubScraper(pages=[_page(50, total=53), _page(3, first_id=2000, total=53)])
    items = sc._collect_board("jefferies", tn.DEFAULT_BOARDS["jefferies"])
    assert len(items) == 53
    assert len(sc.urls) == 2


def test_real_fixture_is_single_page():
    """实测 Jefferies 75 条会翻两页；Fidelity 24 条只有一页（这里用 fixture 验证收尾）。"""
    sc = _StubScraper(pages=[_fixture("fidelity")])
    items = sc._collect_board("fidelity", tn.DEFAULT_BOARDS["fidelity"])
    assert len(items) == 7
    assert len(sc.urls) == 1, "不足 50 条那一页就该收手"


def test_pagination_never_exceeds_max_pages():
    pages = [_page(50, first_id=i * 1000, total=100000) for i in range(20)]
    sc = _StubScraper(pages=pages)
    sc._collect_board("jefferies", tn.DEFAULT_BOARDS["jefferies"])
    assert len(sc.urls) == tn.MAX_PAGES_PER_BOARD


def test_wrapped_page_stops_pagination():
    """翻到底后站点回卷到第一页（同一批 id 再来一遍）→ 立刻收手。"""
    same = _page(50, total=100000)
    sc = _StubScraper(pages=[same, same, same])
    items = sc._collect_board("jefferies", tn.DEFAULT_BOARDS["jefferies"])
    assert len(items) == 50
    assert len(sc.urls) == 2


# ---------------------------------------------------------------------------
# 4. JobPosting 字段拼装
# ---------------------------------------------------------------------------
def test_job_fields_are_assembled_correctly():
    sc = _StubScraper(pages=[_fixture("jefferies")], config=_cfg())
    jobs = sc._fetch_board("jefferies", tn.DEFAULT_BOARDS["jefferies"])
    # fixture 里 6 条磁贴，其中 4 条标题带 Hong Kong
    assert len(jobs) == 4
    job = next(j for j in jobs if "Cash Trading" in j.title)
    assert job.source == "talnet"
    assert job.external_id == "jefferies-1997"
    assert job.company == tn.DEFAULT_BOARDS["jefferies"]["name"]
    assert job.url.startswith("https://jefferies.tal.net/")
    assert job.url_normalized.startswith("https://jefferies.tal.net/")
    assert job.location == "Hong Kong"          # 卡片无地点字段时的兜底
    assert job.posted_at is None                 # 站上没有发布时间，不编造
    assert job.description_snippet == ""         # 列表页没有摘要
    assert job.job_type == ""                    # 交给 classify_posting 推断
    assert job.direct_apply is True


def test_fidelity_gives_four_hk_jobs():
    sc = _StubScraper(pages=[_fixture("fidelity")])
    jobs = sc._fetch_board("fidelity", tn.DEFAULT_BOARDS["fidelity"])
    titles = sorted(j.title for j in jobs)
    assert len(jobs) == 4, titles
    assert all("Hong Kong" in t for t in titles)


def test_posted_at_extractor_never_invents_a_time():
    """父类抽象方法必须有实现，但不能返回 now_hk()。"""
    assert TalNetScraper(_cfg(), browser=None)._extract_posted_at("") is None
    assert TalNetScraper(_cfg(), browser=None)._extract_posted_at("30 days ago") is None


# ---------------------------------------------------------------------------
# 5. 单家失败不拖垮其他家；全失败要报 error
# ---------------------------------------------------------------------------
def _two_boards() -> dict:
    return {
        "a": {"name": "A 投行", "host": "a.tal.net", "board": "/vx/lang-en-GB/candidate/jobboard/vacancy/1/adv/",
              "enabled": True},
        "b": {"name": "B 资管", "host": "b.tal.net", "board": "/vx/lang-en-GB/candidate/jobboard/vacancy/1/adv/",
              "enabled": True},
    }


def test_one_board_failure_keeps_the_other(monkeypatch):
    from unittest import mock

    monkeypatch.setattr(tn, "load_boards", _two_boards)
    sc = _StubScraper()

    def _fetch(key, board):                            # noqa: ARG001
        if key == "a":
            raise urllib.error.HTTPError("https://a.tal.net/x", 503, "boom", {}, None)
        return [sc._to_job("b", board, {"opp_id": "7", "title": "Summer Analyst - Hong Kong",
                                        "url": "/opp/7", "location": ""})]

    monkeypatch.setattr(sc, "_fetch_board", _fetch)
    with mock.patch.object(tn, "logger") as fake_logger:
        jobs = sc._do_fetch(now_hk() - timedelta(days=7))
    assert [j.external_id for j in jobs] == ["b-7"]
    assert fake_logger.error.call_args_list == [], "只失败一家不该报 error"


def test_all_boards_failed_logs_actionable_error(monkeypatch):
    from unittest import mock

    monkeypatch.setattr(tn, "load_boards", _two_boards)
    sc = _StubScraper()

    def _fetch(key, board):                            # noqa: ARG001
        raise urllib.error.HTTPError("https://a.tal.net/x", 503, "boom", {}, None)

    monkeypatch.setattr(sc, "_fetch_board", _fetch)
    with mock.patch.object(tn, "logger") as fake_logger:
        assert sc._do_fetch(now_hk()) == []
    msg = " ".join(str(c.args[0]) for c in fake_logger.error.call_args_list)
    assert "0 条" in msg
    assert "a.tal.net" in msg and "jobboard" in msg      # 能定位到 host 与 board
    assert "503" in msg                                  # 带状态码
    assert "talnet_boards.yaml" in msg                   # 告诉用户下一步改哪


def test_no_enabled_board_returns_empty_without_error(monkeypatch):
    from unittest import mock

    monkeypatch.setattr(tn, "load_boards",
                        lambda: {"a": {**tn.DEFAULT_BOARDS["jefferies"], "enabled": False}})
    sc = _StubScraper()
    with mock.patch.object(tn, "logger") as fake_logger:
        assert sc._do_fetch(now_hk()) == []
    assert fake_logger.error.call_args_list == []


# ---------------------------------------------------------------------------
# 6. board 清单：内置 + 用户文件「逐条合并」
# ---------------------------------------------------------------------------
def test_boards_merge_keeps_builtins_and_allows_disable():
    original = tn.BOARDS_FILE
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "talnet_boards.yaml"
        path.write_text(
            "boards:\n"
            "  fidelity:\n"
            "    enabled: false\n"
            "  myshop:\n"
            "    name: 我的目标公司\n"
            "    host: myshop.tal.net\n"
            "    board: /vx/lang-en-GB/candidate/jobboard/vacancy/3/adv/\n",
            encoding="utf-8")
        try:
            tn.BOARDS_FILE = path
            merged = tn.load_boards()
        finally:
            tn.BOARDS_FILE = original
    assert "jefferies" in merged and "myshop" in merged     # 内置的另一家还在
    assert merged["fidelity"]["enabled"] is False           # 同名 key 以文件为准
    assert merged["fidelity"]["host"] == "fidelityinternational.tal.net"   # 没覆盖的字段保留
    assert merged["myshop"]["name"] == "我的目标公司"


def test_missing_boards_file_uses_builtins():
    original = tn.BOARDS_FILE
    try:
        tn.BOARDS_FILE = Path(tempfile.gettempdir()) / "never_exists_boards_xyz.yaml"
        merged = tn.load_boards()
    finally:
        tn.BOARDS_FILE = original
    assert set(merged) >= {"jefferies", "fidelity"}
    assert all(v.get("enabled") for v in merged.values())


def test_broken_boards_yaml_falls_back_to_builtins():
    original = tn.BOARDS_FILE
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "talnet_boards.yaml"
        path.write_text("boards: [unclosed", encoding="utf-8")
        try:
            tn.BOARDS_FILE = path
            merged = tn.load_boards()
        finally:
            tn.BOARDS_FILE = original
    assert "jefferies" in merged


# ---------------------------------------------------------------------------
# 7. HTTP：UA / 编码 / 重试 / 限速
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def test_request_sends_chrome_ua_and_identity_encoding(monkeypatch):
    """不能带爬虫 UA（站点 robots 明令屏蔽），也不能要 gzip（urllib 不解压）。"""
    import urllib.request

    captured = {}

    def _fake_urlopen(req, timeout=0):                 # noqa: ARG001
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp("<html>ok</html>".encode())

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(tn, "REQUEST_INTERVAL_SECONDS", 0)
    sc = TalNetScraper(_cfg(), browser=None)
    assert sc._http_get("https://jefferies.tal.net/x") == "<html>ok</html>"
    ua = captured["headers"]["user-agent"]
    assert "Chrome/" in ua
    assert not any(bot in ua for bot in ("ClaudeBot", "GPTBot", "Bytespider", "HeadlessChrome"))
    assert captured["headers"]["accept-encoding"] == "identity"


def test_request_paces_between_calls(monkeypatch):
    """robots.txt 的 Crawl-delay 折中：每次请求后停一会儿。"""
    import urllib.request

    slept: list[float] = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(b"ok"))
    monkeypatch.setattr(tn.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(tn, "REQUEST_INTERVAL_SECONDS", 0.5)
    TalNetScraper(_cfg(), browser=None)._http_get("https://jefferies.tal.net/x")
    assert slept == [0.5]


def test_4xx_is_not_retried(monkeypatch):
    import urllib.request

    calls: list[str] = []

    def _boom(req, timeout=0):                         # noqa: ARG001
        calls.append("x")
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(tn.time, "sleep", lambda s: None)
    monkeypatch.setattr(tn, "REQUEST_INTERVAL_SECONDS", 0)
    try:
        TalNetScraper(_cfg(), browser=None)._http_get("https://jefferies.tal.net/x")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:                                              # pragma: no cover
        raise AssertionError("404 必须抛出来（地址写错，重试没意义）")
    assert len(calls) == 1


def test_5xx_is_retried_then_raised(monkeypatch):
    import urllib.request

    calls: list[str] = []

    def _boom(req, timeout=0):                         # noqa: ARG001
        calls.append("x")
        raise urllib.error.HTTPError(req.full_url, 503, "boom", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(tn.time, "sleep", lambda s: None)
    monkeypatch.setattr(tn, "REQUEST_INTERVAL_SECONDS", 0)
    try:
        TalNetScraper(_cfg(), browser=None)._http_get("https://jefferies.tal.net/x")
    except urllib.error.HTTPError as exc:
        assert exc.code == 503
    else:                                              # pragma: no cover
        raise AssertionError("两次尝试都失败后必须抛出")
    assert len(calls) == tn.REQUEST_ATTEMPTS


# ---------------------------------------------------------------------------
# 8. 与关键词初筛的接缝：8 条校招岗都能过类型闸门
# ---------------------------------------------------------------------------
def test_campus_titles_are_classified_as_internship():
    """job_type 留空是刻意的：Summer Analyst / Off-Cycle 由 classify_posting 认。

    这 8 条全部是 2027 校招实习。若这里某条被判成 full time 或空，
    它会在 student 阶段被类型安全网剔掉——抓到了却看不到，等于白抓。
    """
    from hk_job_radar.models import JobPosting

    titles = [
        "2027 Equities Sales & Trading Summer Analyst Program - Cash Trading - Hong Kong",
        "2027 Equities Sales & Trading Summer Analyst – High Touch Trading – Hong Kong",
        "2027 Information Technology Summer Analyst (Infrastructure) - Hong Kong",
        "2027 Information Technology Summer Analyst (Equity Technology) - Hong Kong",
        "Equity Research MBA Intern Programme 2027 - Hong Kong",
        "General Counsel Graduate Programme 2027 - Hong Kong",
        "Business  Off-Cycle Internship Programme 2027 - Hong Kong",
        "Fixed Income Off-Cycle Internship Programme 2027 - Hong Kong",
    ]
    kinds = {}
    for t in titles:
        job = JobPosting(source="talnet", external_id="x", title=t, company="c",
                         url="https://x/1", url_normalized="https://x/1")
        kinds[t] = KeywordFilter.classify_posting(job)
    # General Counsel 是毕业生项目（graduate programme，任何阶段都放行）
    counsel = "General Counsel Graduate Programme 2027 - Hong Kong"
    assert kinds[counsel] == "graduate programme"
    for t in titles:
        if t != counsel:
            assert kinds[t] == "internship", f"{t} → {kinds[t]}"
