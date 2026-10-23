"""已投记录同步（P3.5）测试：解析、接口、匹配、写库保护。

重点不是「能不能跑」，而是**不会把没投的标成已投**：
- 优先用 `jobsdb:<编号>` 直接对 uid（标题被改过也能对上）；
- 只认同站的库行（LinkedIn 上抓到的同岗位不算你投过）；
- 不覆盖用户自己标过的「约面/结束/不感兴趣」；
- 匹配不上就只报告；
- 重复跑不重复写历史，且历史时间是**真实投递时间**而不是同步时间。

兼容两种运行方式：pytest 与 `python tests/test_applied_sync.py`。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hk_job_radar.applied_sync import (                     # noqa: E402
    AppliedEntry,
    SyncReport,
    apply_entries,
    collect_applied,
    ensure_profile,
    entry_from_node,
    has_login,
    parse_applied_date,
    parse_card,
)
from hk_job_radar.db import Database                        # noqa: E402
from hk_job_radar.models import JobPosting                 # noqa: E402

# 真实卡片文本（来自 JobsDB 的 Activity → Applied，原样复制）。
# 注意标题里的 `‑` 是 U+2011 非断字连字符，站点就是这么发的。
REAL_CARD = """Job Title
AI Investment‑Research System Develop (full time/intern)
Advertiser
ZWC Management Limited
Location
Central and Western District
Applied on Jobsdb
21 Oct 2026
twenty-one October, two thousand and twenty-six
Show strong interest"""

# 真实的 GraphQL 响应骨架（字段名照抄，值做过裁剪）。
GRAPHQL_PAYLOAD = {"data": {"viewer": {"id": 601110187, "appliedJobs": {"edges": [
    {"node": {
        "id": "95330658",
        "isActive": True,
        "isExternal": False,
        "appliedAt": {"dateTimeUtc": "2026-10-21T07:46:51.006Z"},
        "job": {"id": "95330658",
                "title": "Data Analyst",
                "location": {"label": "Central and Western District"},
                "advertiser": {"name": "ACME Limited"}},
    }},
    {"node": {
        "id": "95049569",
        "isActive": True,
        "isExternal": True,
        "appliedAt": {"dateTimeUtc": "2026-10-07T02:10:00.000Z"},
        "job": {"id": "95049569",
                "title": "Business Analyst",
                "location": {"label": "Kwun Tong"},
                "advertiser": {"name": "HKBN JOS Limited"}},
    }},
]}}}}


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
class _TempDB:
    def __enter__(self) -> Database:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "jobs.db"))
        self.db.init_schema()
        return self.db

    def __exit__(self, *exc) -> None:
        self.db.close()
        self._dir.cleanup()


def _job(n: int, title: str = "Data Analyst", company: str = "ACME",
         status: str = "new", source: str = "jobsdb") -> JobPosting:
    posted = datetime.now(timezone.utc) - timedelta(days=1)
    return JobPosting(
        source=source, external_id=str(n), title=title, company=company,
        url=f"https://example.com/{n}", url_normalized=f"https://example.com/{n}",
        posted_at=posted, description_snippet="SQL.", llm_score=80,
        llm_reason="", status=status, eligibility_note="", first_seen_at=posted,
    )


def _history_count(db: Database) -> int:
    return int(db.conn.execute("SELECT COUNT(*) FROM status_history").fetchone()[0])


def _fake_cookie_db(path: Path, names: list[str], host: str = "hk.jobsdb.com") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        # 同一个档案可能被写两次（先造游客态、再造登录态），所以先清表
        conn.execute("DROP TABLE IF EXISTS cookies")
        conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
        conn.executemany("INSERT INTO cookies VALUES (?,?,'x')",
                         [(host, n) for n in names])
        conn.commit()
    finally:
        conn.close()


class _FakeMouse:
    def wheel(self, *args) -> None:
        return None


class _FakeRequest:
    method = "POST"


class _FakeResponse:
    """假的 Playwright Response：只要 json() 和 url / request.method。"""

    def __init__(self, payload: dict) -> None:
        self.url = "https://hk.jobsdb.com/graphql"
        self.request = _FakeRequest()
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakePage:
    """只实现 collect_applied 用到的那几个方法（鸭子类型，不碰真浏览器）。

    `graphql` 给定时，第一次 `goto` 会触发 response 监听——模拟页面加载过程
    中自己发出的 appliedJobs 请求；不给则走 DOM 兜底路径。
    """

    def __init__(self, by_page: dict[int, list[str]] | None = None,
                 graphql: dict | None = None) -> None:
        self.by_page = by_page or {}
        self.graphql = graphql
        self.mouse = _FakeMouse()
        self.listeners: list = []
        self.visited: list[str] = []
        self._cards: list[str] = []

    def on(self, event: str, handler) -> None:
        if event == "response":
            self.listeners.append(handler)

    def goto(self, url: str, **kwargs) -> None:
        self.visited.append(url)
        if self.graphql is not None and len(self.visited) == 1:
            for handler in self.listeners:
                handler(_FakeResponse(self.graphql))
            return
        number = 1
        if "page=" in url:
            number = int(url.rsplit("page=", 1)[1])
        self._cards = self.by_page.get(number, [])

    def wait_for_timeout(self, ms: int) -> None:
        return None

    def evaluate(self, js: str):
        # 滚动的状态探测：直接告诉它「已经到底了」，避免测试里真的循环滚动
        if "scrollY" in js:
            return {"y": 99999, "h": 100, "w": 900}
        return self._cards


# ---------------------------------------------------------------------------
# 卡片文本解析（DOM 兜底路径）
# ---------------------------------------------------------------------------
def test_parse_card_reads_every_field():
    entry = parse_card(REAL_CARD)
    assert entry is not None
    assert entry.title == "AI Investment‑Research System Develop (full time/intern)"
    assert entry.company == "ZWC Management Limited"
    assert entry.location == "Central and Western District"
    assert entry.applied_on == "2026-10-21"
    assert entry.external is False, "「Applied on Jobsdb」不是跳转官网"


def test_parse_card_tolerates_missing_location():
    text = ("Job Title\nIT Intern / Graduate Trainee\nAdvertiser\nSDIC Securities\n"
            "Applied on Jobsdb\n21 Oct 2026")
    entry = parse_card(text)
    assert entry is not None and entry.location == ""
    assert entry.applied_on == "2026-10-21"


def test_parse_card_marks_employer_site_channel():
    """「Applied on employer site」＝跳转到雇主站投的，要认出来并标记。"""
    text = ("Job Title\nData Analyst\nAdvertiser\nACME\n"
            "Applied on employer site\n3 Sep 2026")
    entry = parse_card(text)
    assert entry is not None and entry.external is True


def test_parse_card_rejects_non_card_text():
    """页头的宣传块、空文本都不该被当成一条投递。"""
    banner = ("Show strong interest in the jobs you really want\n"
              "You can mark 3 jobs applied on Jobsdb in the last 30 days.")
    assert parse_card(banner) is None
    assert parse_card("") is None
    assert parse_card("Job Title\nOnly a title, no advertiser") is None


def test_parse_card_strips_invisible_characters():
    """站点会在文本节点里塞 WORD JOINER（U+2060），不剥掉会带进职位名里。"""
    text = ("Job Title\nData\u2060 Analyst\u200b\nAdvertiser\nACME\u2060\n"
            "Applied on Jobsdb\n21 Oct 2026")
    entry = parse_card(text)
    assert entry is not None
    assert entry.title == "Data Analyst"
    assert entry.company == "ACME"


def test_parse_applied_date_variants():
    assert parse_applied_date("21 Oct 2026") == "2026-10-21"
    assert parse_applied_date("1 September 2026") == "2026-09-01"
    assert parse_applied_date("31 Feb 2026") == "", "非法日期要留空，不能抛"
    assert parse_applied_date("") == ""
    assert parse_applied_date("二十一日") == ""


def test_entry_key_normalizes_company_suffix():
    """同一个岗位在不同站写法不同（Ltd / Limited），归一化后 key 必须相等。"""
    a = AppliedEntry(title="Data Analyst", company="ACME Ltd")
    b = AppliedEntry(title="Data Analyst", company="ACME Limited")
    assert a.key == b.key


# ---------------------------------------------------------------------------
# GraphQL 主路径
# ---------------------------------------------------------------------------
def test_entry_from_node_reads_real_fields():
    node = GRAPHQL_PAYLOAD["data"]["viewer"]["appliedJobs"]["edges"][0]["node"]
    entry = entry_from_node(node)
    assert entry is not None
    assert entry.job_id == "95330658"
    assert entry.uid == "jobsdb:95330658", "uid 必须就是库里的主键形式"
    assert entry.title == "Data Analyst"
    assert entry.company == "ACME Limited"
    assert entry.applied_on == "2026-10-21"
    assert entry.external is False


def test_entry_from_node_flags_external_application():
    node = GRAPHQL_PAYLOAD["data"]["viewer"]["appliedJobs"]["edges"][1]["node"]
    entry = entry_from_node(node)
    assert entry is not None and entry.external is True


def test_entry_when_is_hong_kong_time():
    """接口给的是 UTC，进度页按香港时间算「近 7 天」，必须转过来。"""
    node = GRAPHQL_PAYLOAD["data"]["viewer"]["appliedJobs"]["edges"][0]["node"]
    entry = entry_from_node(node)
    assert entry is not None
    when = entry.when
    assert when is not None
    assert when.hour == 15 and when.minute == 46, f"07:46 UTC 应是 15:46 HK，得到 {when}"
    assert entry.applied_on == when.date().isoformat()


def test_entry_when_returns_none_without_timestamp():
    assert AppliedEntry(title="A", company="B").when is None
    assert AppliedEntry(title="A", company="B", applied_at="not-a-date").when is None


def test_collect_prefers_graphql():
    page = _FakePage(graphql=GRAPHQL_PAYLOAD)
    entries, pages, via = collect_applied(page, timeout_s=0)
    assert via == "graphql"
    assert len(entries) == 2 and pages == 1
    assert [e.job_id for e in entries] == ["95330658", "95049569"]
    assert len(page.visited) == 1, "接口拿到就不该再翻页读 DOM"


# ---------------------------------------------------------------------------
# DOM 兜底路径
# ---------------------------------------------------------------------------
def _card(title: str, company: str) -> str:
    return f"Job Title\n{title}\nAdvertiser\n{company}\nApplied on Jobsdb\n21 Oct 2026"


def test_collect_applied_pages_and_dedupes():
    page = _FakePage({
        1: [_card("A", "X"), _card("B", "Y")],
        2: [_card("C", "Z")],
        3: [_card("A", "X"), _card("B", "Y")],      # 越界页码会回卷到第一页
    })
    entries, pages, via = collect_applied(page, timeout_s=0, max_pages=8)
    assert via == "dom", "没有接口响应时只能退到读页面"
    assert [e.title for e in entries] == ["A", "B", "C"]
    assert pages == 2, "回卷那页全是旧数据，不该算一页"
    assert len(page.visited) == 3, "发现整页重复后要停下来，不能一直翻"


def test_collect_applied_stops_on_empty_page():
    page = _FakePage({1: [_card("A", "X")], 2: []})
    entries, pages, _ = collect_applied(page, timeout_s=0, max_pages=5)
    assert len(entries) == 1 and pages == 1


def test_collect_applied_survives_duplicate_cards_in_one_page():
    """同一页里出现两张一模一样的卡（站点偶发）只算一条。"""
    page = _FakePage({1: [_card("A", "X"), _card("A", "X")]})
    entries, _, _ = collect_applied(page, timeout_s=0, max_pages=3)
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# 匹配与写库
# ---------------------------------------------------------------------------
def test_apply_matches_by_job_id_even_if_title_changed():
    """招聘方改了标题也不影响：编号对得上就是同一条。这是接口路径的价值。"""
    with _TempDB() as db:
        db.upsert_job(_job(1, title="Data Analyst (Senior)", company="ACME Limited"))
        entry = AppliedEntry(title="Data Analyst", company="ACME Limited",
                             job_id="1", applied_on="2026-10-21")
        report = SyncReport(found=1)
        apply_entries(db, [entry], report)
        assert db.get_job("jobsdb:1").status == "applied"
        assert len(report.marked) == 1 and not report.unmatched


def test_apply_falls_back_to_company_and_title():
    """没编号的记录（DOM 兜底）只靠文字，但归一化后必须完全相等。"""
    with _TempDB() as db:
        db.upsert_job(_job(1, title="Data Analyst", company="ACME Ltd",
                           status="reported"))
        entry = AppliedEntry(title="Data Analyst", company="ACME Limited",
                             applied_on="2026-10-21")
        report = SyncReport(found=1)
        apply_entries(db, [entry], report)
        assert db.get_job("jobsdb:1").status == "applied"
        assert _history_count(db) == 1
        note = db.conn.execute("SELECT note FROM status_history").fetchone()[0]
    assert "2026-10-21" in note, "历史里要留真实投递日期"


def test_apply_ignores_same_job_on_other_sites():
    """同一岗位在 LinkedIn 也抓到过，但那次不是你在 JobsDB 投的。"""
    with _TempDB() as db:
        db.upsert_job(_job(1, source="jobsdb"))
        db.upsert_job(_job(2, source="linkedin"))
        entry = AppliedEntry(title="Data Analyst", company="ACME",
                             applied_on="2026-10-21")
        report = SyncReport(found=1)
        apply_entries(db, [entry], report)
        assert db.get_job("jobsdb:1").status == "applied"
        assert db.get_job("linkedin:2").status == "new", "别的站不该被改"
        assert len(report.marked) == 1


def test_apply_keeps_user_decisions():
    """约面/结束/不感兴趣都是用户自己的判断，同步不能把它们拉回「已投」。"""
    with _TempDB() as db:
        db.upsert_job(_job(1, title="A", status="interview"))
        db.upsert_job(_job(2, title="B", status="rejected"))
        db.upsert_job(_job(3, title="C", status="closed"))
        entries = [AppliedEntry(title=t, company="ACME", job_id=str(i))
                   for i, t in enumerate("ABC", start=1)]
        report = SyncReport(found=3)
        apply_entries(db, entries, report)
        assert [db.get_job(f"jobsdb:{i}").status for i in (1, 2, 3)] == \
            ["interview", "rejected", "closed"]
        assert len(report.kept) == 3 and not report.marked
        assert _history_count(db) == 0, "没改状态就不该写历史"


def test_apply_is_idempotent():
    with _TempDB() as db:
        db.upsert_job(_job(1))
        entry = AppliedEntry(title="Data Analyst", company="ACME", job_id="1",
                             applied_on="2026-10-21")
        first = SyncReport(found=1)
        apply_entries(db, [entry], first)
        second = SyncReport(found=1)
        apply_entries(db, [entry], second)
        assert len(first.marked) == 1
        assert not second.marked and second.already == 1
        assert _history_count(db) == 1, "重复同步不该叠历史"


def test_apply_uses_real_applied_time_in_history():
    """补录要用真实投递时间：「近 7 天投递」按历史时间线算，不能用同步时间。"""
    node = GRAPHQL_PAYLOAD["data"]["viewer"]["appliedJobs"]["edges"][0]["node"]
    entry = entry_from_node(node)
    assert entry is not None
    with _TempDB() as db:
        db.upsert_job(_job(1))
        report = SyncReport(found=1)
        apply_entries(db, [entry], report)
        created = db.conn.execute(
            "SELECT created_at FROM status_history").fetchone()[0]
    assert created.startswith("2026-10-21T15:46"), f"应是香港时间，得到 {created}"


def test_apply_reports_unmatched_without_touching_db():
    with _TempDB() as db:
        db.upsert_job(_job(1, title="Something Else"))
        entry = AppliedEntry(title="Data Analyst", company="ACME")
        report = SyncReport(found=1)
        apply_entries(db, [entry], report)
        assert report.unmatched == [entry]
        assert not report.marked
        assert db.get_job("jobsdb:1").status == "new"


def test_dry_run_writes_nothing():
    with _TempDB() as db:
        db.upsert_job(_job(1))
        entry = AppliedEntry(title="Data Analyst", company="ACME")
        report = SyncReport(found=1)
        apply_entries(db, [entry], report, dry_run=True)
        assert len(report.marked) == 1, "报告里照样要算「本来会标几条」"
        assert db.get_job("jobsdb:1").status == "new"
        assert _history_count(db) == 0


def test_report_summary_mentions_every_bucket():
    report = SyncReport(pages=5, found=94, via="dom")
    report.marked.append(("jobsdb:1", AppliedEntry("A", "X")))
    report.already = 10
    report.kept.append(("jobsdb:2", "rejected", AppliedEntry("B", "Y")))
    report.unmatched.append(AppliedEntry("C", "Z"))
    text = report.summary()
    for word in ("5 页", "94 条", "10 条"):
        assert word in text


# ---------------------------------------------------------------------------
# 补录（--backfill）：站点上有、雷达没抓到的那些
# ---------------------------------------------------------------------------
def _untracked(job_id: str = "999", when: str = "2026-10-07") -> AppliedEntry:
    """一条「站点上有、库里没有」的投递记录。"""
    return AppliedEntry(
        title="Senior Data Analyst", company="Some Bank Ltd",
        job_id=job_id, location="Hong Kong",
        applied_on=when, applied_at=f"{when}T03:00:00.000Z")


def test_backfill_is_off_by_default():
    """默认绝不动库：往库里添几十行是用户该自己决定的事。"""
    with _TempDB() as db:
        entry = _untracked()
        report = SyncReport(found=1)
        apply_entries(db, [entry], report)
        assert report.unmatched == [entry]
        assert not report.backfilled
        assert db.get_job("jobsdb:999") is None
        assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_backfill_inserts_the_missing_job():
    """补录进来的行要有链接、真实投递日期，状态直接是已投。"""
    with _TempDB() as db:
        entry = _untracked()
        report = SyncReport(found=1)
        apply_entries(db, [entry], report, backfill=True)

        assert len(report.backfilled) == 1
        assert not report.unmatched, "补上了就不该再算「对不上」"
        job = db.get_job("jobsdb:999")
        assert job is not None
        assert job.status == "applied"
        assert job.url == "https://hk.jobsdb.com/job/999", "得能点开原链接"
        assert job.llm_score is None, "不为了补录去调模型打分"
        assert job.description_snippet == ""
        assert job.dedupe_key, "指纹要存好，之后跨站去重要用"


def test_backfill_keeps_the_real_applied_date():
    """历史时间线必须是真实投递日，否则「近 7 天投递」全算成今天。"""
    with _TempDB() as db:
        entry = _untracked(when="2026-10-07")
        apply_entries(db, [entry], SyncReport(found=1), backfill=True)
        created = db.conn.execute(
            "SELECT created_at FROM status_history").fetchone()[0]
        note = db.conn.execute("SELECT note FROM status_history").fetchone()[0]
    assert created.startswith("2026-10-07"), created
    assert "补录" in note, "备注要写清这条不是雷达抓到的"


def test_backfilled_job_never_enters_the_daily_report():
    """★ 补录的岗位没有 JD、没有分数，绝不能出现在下一份日报里。

    `get_unreported_jobs` 只看 reported、不看 status——如果补录时忘了
    把 reported 置 1，日报会冒出一堆「未打分、你早就投过」的岗位，
    把真正待处理的机会挤下去。
    """
    with _TempDB() as db:
        db.upsert_job(_job(1))                   # 正常抓到、还没推
        apply_entries(db, [_untracked()], SyncReport(found=1), backfill=True)
        pending = db.get_unreported_jobs(score_threshold=80, show_unscored=True)
        uids = [j.job_uid for j in pending]
    assert uids == ["jobsdb:1"], f"补录那条不该进日报，得到 {uids}"


def test_backfill_skips_entries_without_job_id():
    """没有岗位编号就拼不出链接，补进来是个点不开的空壳——绝不补。"""
    with _TempDB() as db:
        entry = AppliedEntry(title="Data Analyst", company="ACME")
        report = SyncReport(found=1)
        apply_entries(db, [entry], report, backfill=True)
        assert not report.backfilled
        assert report.unmatched == [entry]
        assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_backfill_dry_run_writes_nothing():
    with _TempDB() as db:
        report = SyncReport(found=1)
        apply_entries(db, [_untracked()], report, dry_run=True, backfill=True)
        assert len(report.backfilled) == 1, "试跑也要报「本来会补几条」"
        assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert _history_count(db) == 0


def test_backfill_does_not_duplicate_on_second_run():
    """连跑两次：第二次靠 uid 直接对上，不再是「对不上」。"""
    with _TempDB() as db:
        apply_entries(db, [_untracked()], SyncReport(found=1), backfill=True)
        second = SyncReport(found=1)
        apply_entries(db, [_untracked()], second, backfill=True)
        total = db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert total == 1, "第二次应该命中已有行，不能插出第二条"
    assert not second.backfilled
    assert second.already == 1


def test_report_detail_lines_group_kept_apart_from_unmatched():
    """明细要分组：把「你标过更靠后状态」和「库里没有」混在一起，
    用户没法判断哪些需要自己动手。"""
    report = SyncReport(found=3)
    report.kept.append(("jobsdb:2", "rejected", AppliedEntry("B", "Y")))
    report.unmatched.append(AppliedEntry("C", "Z"))
    lines = report.detail_lines()
    assert any("rejected" in line for line in lines)


def test_report_summary_marks_dom_fallback_as_incomplete():
    """兜底路径可能读不全，必须说出来，不能让用户以为 46 条就是全部。"""
    report = SyncReport(pages=2, found=46, via="dom")
    assert "不全" in report.summary()
    assert "不全" not in SyncReport(found=94, via="graphql").summary()


# ---------------------------------------------------------------------------
# 浏览器档案
# ---------------------------------------------------------------------------
def test_has_login_detects_auth0_marker():
    with tempfile.TemporaryDirectory() as d:
        profile = Path(d) / "p"
        _fake_cookie_db(profile / "Default" / "Network" / "Cookies",
                        ["_ga", "appSession"])
        assert has_login(profile) is False, "游客档案没有 authenticated 标记"

        _fake_cookie_db(profile / "Default" / "Network" / "Cookies",
                        ["_ga", "auth0.s4xK.is.authenticated"])
        assert has_login(profile) is True
    assert has_login(Path(d) / "not-there") is False


def test_ensure_profile_copies_logged_in_sibling():
    """用户换过安装位置时登录态留在旧目录：就近复制一次，不用他翻文件夹。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _fake_cookie_db(root / "hk_job_radar" / "data" / "browser" / "jobsdb"
                        / "Default" / "Network" / "Cookies",
                        ["auth0.s4xK.is.authenticated"])
        pro_browser = root / "hk_job_radar_pro" / "data" / "browser"
        pro_browser.mkdir(parents=True)

        note = ensure_profile(pro_browser)
        assert "hk_job_radar" in note
        assert has_login(pro_browser / "jobsdb"), "复制后要真的算已登录"
        assert ensure_profile(pro_browser) == "", "第二次什么都不做"


def test_ensure_profile_does_not_copy_guest_profile():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _fake_cookie_db(root / "hk_job_radar" / "data" / "browser" / "jobsdb"
                        / "Default" / "Network" / "Cookies", ["_ga"])
        pro_browser = root / "hk_job_radar_pro" / "data" / "browser"
        pro_browser.mkdir(parents=True)
        assert ensure_profile(pro_browser) == ""
        assert not (pro_browser / "jobsdb").exists(), "没登录就别复制，省得误导"


def test_ensure_profile_keeps_existing_target():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _fake_cookie_db(root / "free" / "data" / "browser" / "jobsdb"
                        / "Default" / "Network" / "Cookies",
                        ["auth0.x.is.authenticated"])
        pro_browser = root / "pro" / "data" / "browser"
        (pro_browser / "jobsdb" / "Default").mkdir(parents=True)
        (pro_browser / "jobsdb" / "Default" / "marker.txt").write_text("mine")
        assert ensure_profile(pro_browser) == ""
        assert (pro_browser / "jobsdb" / "Default" / "marker.txt").exists(), \
            "已有档案不能被覆盖"


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
