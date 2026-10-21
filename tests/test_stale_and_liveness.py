"""过期岗位的两道闸门 + 卡片时效标注：单元测试。

为什么值得单独写一组测试：这部分防的是「用户点开链接发现已过期」这种
信任级故障——它不会报错、不会崩溃，只会悄悄发生，然后在用户心里把产品
从「有点用」降级成「不靠谱」。而这类问题恰恰最容易在改动中悄悄回归：

- 有人把 `get_unreported_jobs()` 的 `max_age_days` 参数漏传 → 闸门失效；
- 有人改了时间存储格式（比如去掉时区） → SQLite 的 datetime() 解析失败，
  闸门静默失效（比较结果全是 NULL，一条也不会被归档）；
- 有人给 LinkedIn 也加了详情页复核 → 触碰合规红线。

所以这里既测「正常路径」，也测「格式被改坏时的防御」。

兼容两种运行方式：
- pytest tests/test_stale_and_liveness.py
- python tests/test_stale_and_liveness.py
"""
from __future__ import annotations

import sys
import tempfile
import sqlite3
from datetime import timedelta
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar import liveness
from hk_job_radar.db import Database
from hk_job_radar.models import JobPosting, now_hk, to_iso
from hk_job_radar.notify.report import (
    build_feedback_row,
    build_job_card,
    _age_line,
)


# ---------------------------------------------------------------------------
# 造数据的小工具
# ---------------------------------------------------------------------------
def _job(source: str = "jobsdb", uid: str = "1", *,
         posted_days_ago: float | None = None,
         seen_days_ago: float = 0.0,
         score: int | None = 75, url: str = "") -> JobPosting:
    """造一个岗位：posted_at / first_seen_at 按「多少天前」相对当前时间给。"""
    now = now_hk()
    return JobPosting(
        source=source,
        external_id=uid,
        title=f"Title {uid}",
        company="ACME",
        url=url or f"https://example.com/{source}/{uid}",
        url_normalized=f"https://example.com/{source}/{uid}",
        posted_at=(now - timedelta(days=posted_days_ago)) if posted_days_ago is not None else None,
        first_seen_at=now - timedelta(days=seen_days_ago),
        llm_score=score,
    )


class _DB:
    """临时库上下文（Windows 上不关连接会锁住临时目录，必须 close）。"""

    def __enter__(self) -> Database:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._tmp.name) / "t.db"))
        return self.db

    def __exit__(self, *exc) -> None:
        self.db.close()
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# 一、时间比较：闸门的地基
# ---------------------------------------------------------------------------
def test_sqlite_parses_our_iso_format():
    """库里的时间格式必须能被 SQLite 的 datetime() 解析。

    这条是「闸门失效」的哨兵：如果哪天 to_iso() 改成不带时区、或换成
    2026/10/20 这种写法，datetime() 会返回 NULL，比较结果全是 NULL，
    archive_stale_jobs() 就一条也归档不了——而程序照常运行，没有任何报错。
    """
    conn = sqlite3.connect(":memory:")
    value = to_iso(now_hk())
    parsed = conn.execute("SELECT datetime(?)", (value,)).fetchone()[0]
    conn.close()
    assert parsed is not None, f"SQLite 无法解析时间格式：{value!r}"


def test_archive_stale_jobs_archives_only_old_ones():
    with _DB() as db:
        db.upsert_job(_job(uid="old", posted_days_ago=9))       # 早就过期
        db.upsert_job(_job(uid="mid", posted_days_ago=4))        # 超过 3 天
        db.upsert_job(_job(uid="fresh", posted_days_ago=1))      # 还新鲜
        db.upsert_job(_job(uid="no-date", posted_days_ago=None, seen_days_ago=0.2))
        # 没发布日、但入库已经 5 天（Indeed 那类抓不到发布时间的岗位）
        db.upsert_job(_job(uid="no-date-old", posted_days_ago=None, seen_days_ago=5))

        archived = db.archive_stale_jobs(3)
        assert archived == 3, f"应归档 3 条，实际 {archived}"

        rows = {r["external_id"]: r for r in
                db.conn.execute("SELECT external_id, status, reported FROM jobs")}
        for uid in ("old", "mid", "no-date-old"):
            assert rows[uid]["status"] == "stale", uid
            assert rows[uid]["reported"] == 1, uid
        for uid in ("fresh", "no-date"):
            assert rows[uid]["status"] == "new", uid
            assert rows[uid]["reported"] == 0, uid

        # 幂等：再跑一次不该重复计数
        assert db.archive_stale_jobs(3) == 0


def test_get_unreported_jobs_respects_max_age():
    with _DB() as db:
        db.upsert_job(_job(uid="old", posted_days_ago=6))
        db.upsert_job(_job(uid="fresh", posted_days_ago=1))

        without = [j.external_id for j in db.get_unreported_jobs(60, True)]
        assert without == ["fresh", "old"] or set(without) == {"fresh", "old"}

        gated = [j.external_id for j in db.get_unreported_jobs(60, True, max_age_days=3)]
        assert gated == ["fresh"], f"过龄岗位没被挡住：{gated}"


# ---------------------------------------------------------------------------
# 一·B、双轨年龄线：老不等于失效
# ---------------------------------------------------------------------------
def _dual() -> dict:
    """能复核的站 + 硬天花板，与 pipeline._age_limits() 给的参数同形。"""
    return {
        "recheckable_sources": ("jobsdb", "indeed", "workday"),
        "recheck_age_days": 30,
    }


def test_recheckable_source_survives_the_soft_line():
    """14 天前的 JobsDB 岗位不该被 3 天线砍掉——它要交给复核判活。

    这条是本轮的核心：实测 10 条 14 天前的 JobsDB 岗位里 9 条仍在招，
    按年龄一刀切等于白扔机会。能不能投，要靠打开详情页看一眼，不是靠算天数。
    """
    with _DB() as db:
        db.upsert_job(_job(source="jobsdb", uid="hk-old", posted_days_ago=14))
        assert db.archive_stale_jobs(3, **_dual()) == 0, "能复核的岗位不该按软线归档"
        left = [j.external_id for j in
                db.get_unreported_jobs(60, True, max_age_days=3, **_dual())]
        assert left == ["hk-old"], f"能复核的岗位被软线挡掉了：{left}"


def test_unrecheckable_source_still_cut_by_age():
    """LinkedIn 复核不了，只能靠年龄兜——同样 14 天的岗位要被砍掉。"""
    with _DB() as db:
        db.upsert_job(_job(source="linkedin", uid="li-old", posted_days_ago=14))
        assert db.archive_stale_jobs(3, **_dual()) == 1
        assert db.get_unreported_jobs(60, True, max_age_days=3, **_dual()) == []


def test_recheckable_source_archived_past_hard_ceiling():
    """超过硬天花板（30 天）的连复核都不做，直接归档；20 天的留着。"""
    with _DB() as db:
        db.upsert_job(_job(uid="ancient", posted_days_ago=40))
        db.upsert_job(_job(uid="borderline", posted_days_ago=20))
        assert db.archive_stale_jobs(3, **_dual()) == 1
        left = [j.external_id for j in
                db.get_unreported_jobs(60, True, max_age_days=3, **_dual())]
        assert left == ["borderline"], f"硬天花板失效：{left}"


def test_workday_is_treated_as_recheckable():
    """Workday 的 canApply 等于抓取时就复核过，不该因为年龄被砍。"""
    assert "workday" in liveness.RECHECKABLE_SOURCES
    assert "workday" not in liveness.DETAIL_CHECK_SOURCES, \
        "Workday 抓取时已用 canApply 判过，不该再开详情页（省每轮几十次请求）"


def test_pipeline_age_limits_fall_back_when_check_disabled():
    """关掉复核 → 必须退回单线。

    否则就变成「不复核 + 也不按年龄拦」，一批早就招满的岗位会原样推给用户，
    比不修这个问题更糟。
    """
    from hk_job_radar.config import AppConfig
    from hk_job_radar.pipeline import Pipeline

    off = type("S", (), {"config": AppConfig(liveness_check=False)})()
    assert Pipeline._age_limits(off) == {}, "关了复核还在放宽年龄线"

    on = type("S", (), {"config": AppConfig(liveness_check=True)})()
    limits = Pipeline._age_limits(on)
    assert limits["recheck_age_days"] == liveness.MAX_RECHECK_AGE_DAYS
    assert "jobsdb" in limits["recheckable_sources"]


def test_check_jobs_checks_the_oldest_first():
    """复核预算有限时先查最可能失效的那批（最老的），剩下的才按放行处理。"""
    old = _job(uid="old", posted_days_ago=20)
    new = _job(uid="new", posted_days_ago=1)
    visited: list[str] = []

    class _Page:
        def goto(self, url, wait_until=None, timeout=None):
            visited.append(url)

        def inner_text(self, selector):
            return "normal job description"

    class _Browser:
        def launch(self, source):
            return _Page()

        def wait_cloudflare(self, page, timeout_s=0, quiet=True):
            return True

        def human_delay(self):
            pass

    liveness.check_jobs([new, old], _Browser(), max_checks=1)
    assert visited == [old.url], f"先查的不是最老的：{visited}"


def test_mark_closed_removes_from_queue():
    with _DB() as db:
        db.upsert_job(_job(uid="gone", posted_days_ago=0.1))
        db.mark_closed(["jobsdb:gone"])
        row = db.conn.execute(
            "SELECT status, reported FROM jobs WHERE external_id='gone'").fetchone()
        assert row["status"] == "expired" and row["reported"] == 1
        assert db.get_unreported_jobs(60, True, max_age_days=3) == []


# ---------------------------------------------------------------------------
# 二、关闭文案识别
# ---------------------------------------------------------------------------
def test_is_closed_text_matches_indeed_banner():
    """Indeed 的实际提示（用户截图原文，繁体 + 换行）。"""
    text = "此職位在 Indeed 上已過期\n原因可能是：僱主接受求職申請、沒有積極招聘或正在審閱求職申請中"
    assert liveness.is_closed_text(text)


def test_is_closed_text_matches_linkedin_phrase():
    assert liveness.is_closed_text("5 天前 · 11 位申請者\n目前不接受申請")


def test_is_closed_text_handles_line_breaks_and_spaces():
    """提示语被拆成多行、或中文里塞了全角空格时也要能认出来。"""
    assert liveness.is_closed_text("此職位\n在 Indeed\n上已過期")
    assert liveness.is_closed_text("職位　已過期")
    assert liveness.is_closed_text("No longer accepting applications")


def test_is_closed_text_matches_jobsdb_withdrawn_banner():
    """JobsDB 撤下广告的真实文案（2026-10-20 实测，h2 就是这一句）。"""
    text = ("This job is no longer advertised\n"
            "Jobs remain on Jobsdb for 30 days, unless the advertiser removes them sooner")
    assert liveness.is_closed_text(text)


def test_is_closed_text_survives_disclosed_lookalike():
    """「Salary undisclosed」里含 closed 子串，绝不能因此被当成关闭。

    这条防的是「有人图省事把标记写成裸 closed」——那会让每一条薪资未公开的
    岗位都被当成已关闭，日报直接空掉，而且完全不报错。
    """
    assert not liveness.is_closed_text("Full time · Salary undisclosed · Posted 18d ago")


def test_is_closed_text_ignores_normal_job_description():
    """正常 JD 不能误判——错杀一个好岗位比漏掉一个过期岗位严重得多。"""
    text = ("We are hiring a Data Analyst. 負責數據分析、報表與儀表板。"
            "申請截止日期：2026-12-31。Please apply before the deadline.")
    assert not liveness.is_closed_text(text)
    assert not liveness.is_closed_text("")


#: Indeed 详情页被 Cloudflare 拦死时的真实正文（2026-10-20 实测抓到）
_INDEED_BLOCK_PAGE = (
    "查找职位 / 公司评论 / 查询薪资 / 上传您的简历 / 登录 / 雇主/发布职位 / "
    "请求被拦截 / 您的请求被拦截。如果您认为此判断有误，请访问 support.indeed.com "
    "并提供以下信息： / 您本次请求的 Ray ID 为 a4d7eacc9e6708d1 / "
    "您本次请求的 IP 地址为 144.214.0.6 / 返回主页 / → / Cloudflare 故障排查"
)


def test_block_page_is_not_mistaken_for_closed_job():
    """拦截页必须被识别成「没查成」，绝不能被当成「岗位已关闭」。

    这两种误判的后果完全相反：当成关闭 → 整站好岗位被错杀、日报变空；
    当成没查成 → 只是那一批没过复核，照常推送。所以这条测试是安全底线。
    """
    assert liveness.is_blocked_text(_INDEED_BLOCK_PAGE)
    assert not liveness.is_closed_text(_INDEED_BLOCK_PAGE)


# ---------------------------------------------------------------------------
# 三、复核执行的边界行为
# ---------------------------------------------------------------------------
class _FakeBrowser:
    """假浏览器：一个对象同时充当 BrowserManager 与 Page。"""

    def __init__(self, texts: dict[str, str] | None = None, *, boom: bool = False) -> None:
        self.texts = texts or {}
        self.boom = boom
        self.visited: list[str] = []
        self._current = ""

    # --- BrowserManager 侧 ---
    def launch(self, source_key: str):
        if self.boom:
            raise RuntimeError("profile in use")
        return self

    def wait_cloudflare(self, page, timeout_s: int | None = None,
                        *, quiet: bool = False) -> bool:
        return True

    def human_delay(self) -> None:
        pass

    # --- Page 侧 ---
    def goto(self, url: str, **kwargs) -> None:
        self.visited.append(url)
        self._current = url

    def inner_text(self, selector: str) -> str:
        return self.texts.get(self._current, "正常岗位描述")


def test_check_jobs_never_opens_linkedin():
    """LinkedIn 协议禁止机器人 → 任何情况下都不许打开它的详情页。"""
    jobs = [
        _job("linkedin", "l1", posted_days_ago=0.1),
        _job("jobsdb", "j1", posted_days_ago=0.1),
    ]
    browser = _FakeBrowser()
    result = liveness.check_jobs(jobs, browser)

    assert not any("linkedin" in u for u in browser.visited), browser.visited
    assert result.closed == []
    assert result.skipped == 1          # LinkedIn 那条被跳过
    assert result.checked == 1


def test_check_jobs_detects_closed_and_keeps_others():
    closed_url = "https://example.com/jobsdb/closed"
    jobs = [
        _job("jobsdb", "closed", url=closed_url),
        _job("jobsdb", "alive", url="https://example.com/jobsdb/alive"),
        _job("indeed", "expired", url="https://example.com/indeed/expired"),
    ]
    browser = _FakeBrowser({
        closed_url: "職位已過期",
        "https://example.com/indeed/expired": "此職位在 Indeed 上已過期",
    })
    result = liveness.check_jobs(jobs, browser)

    assert {j.external_id for j in result.closed} == {"closed", "expired"}
    assert result.checked == 3


def test_check_jobs_fails_open_when_browser_cannot_start():
    """浏览器起不来（profile 被占用）→ 全部放行，绝不阻断推送。"""
    jobs = [_job("jobsdb", "a"), _job("jobsdb", "b")]
    result = liveness.check_jobs(jobs, _FakeBrowser(boom=True))
    assert result.closed == []
    assert result.skipped == 2


def test_check_jobs_honours_max_checks():
    jobs = [_job("jobsdb", str(i)) for i in range(5)]
    browser = _FakeBrowser()
    result = liveness.check_jobs(jobs, browser, max_checks=2)
    assert result.checked == 2
    assert result.skipped == 3
    assert len(browser.visited) == 2


def test_check_jobs_stops_the_whole_source_once_blocked():
    """一旦被拦死，同一站点剩下的岗位不再逐个去试（否则每条白等 20 多秒）。"""
    jobs = [_job("indeed", str(i)) for i in range(4)]
    jobs = [JobPosting(source=j.source, external_id=j.external_id, title=j.title,
                       company=j.company, url=f"https://example.com/indeed/{j.external_id}",
                       url_normalized=j.url_normalized) for j in jobs]
    browser = _FakeBrowser({f"https://example.com/indeed/{i}": _INDEED_BLOCK_PAGE
                            for i in range(4)})
    result = liveness.check_jobs(jobs, browser)

    assert result.closed == [], "拦截页绝不能被当成已关闭"
    assert result.checked == 1, f"应在第一条被拦后立即收手，实际查了 {result.checked} 条"
    assert result.skipped == 3
    assert len(browser.visited) == 1


def test_check_jobs_fails_open_when_page_errors():
    class _Broken(_FakeBrowser):
        def goto(self, url: str, **kwargs) -> None:
            raise TimeoutError("navigation timeout")

    jobs = [_job("jobsdb", "x")]
    result = liveness.check_jobs(jobs, _Broken())
    assert result.closed == []


# ---------------------------------------------------------------------------
# 四、卡片上的时效标注与「已关闭」按钮
# ---------------------------------------------------------------------------
def test_age_line_uses_posted_date_when_available():
    assert _age_line(_job(posted_days_ago=0.2)) == ""          # 当天不显示，避免噪音
    assert _age_line(_job(posted_days_ago=1)) == "🕒 昨天发布"
    assert _age_line(_job(posted_days_ago=4)) == "🕒 4 天前发布"


def test_age_line_falls_back_to_first_seen_for_indeed():
    """Indeed 抓不到发布时间 → 文案必须说「发现」而不是「发布」。"""
    assert _age_line(_job("indeed", posted_days_ago=None, seen_days_ago=3)) == "🕒 3 天前发现"


def test_card_shows_age_line():
    card = build_job_card(_job(posted_days_ago=5, score=72), index=1)
    assert "🕒 5 天前发布" in card


def test_feedback_row_has_closed_button():
    row = build_feedback_row(_job(uid="abc"), index=3)
    assert len(row) == 3
    actions = [b["callback_data"].rsplit(":", 1)[-1] for b in row]
    assert actions == ["interested", "not_interested", "expired"]
    assert all(len(b["callback_data"].encode()) <= 64 for b in row), "Telegram 回调数据上限 64 字节"


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
