"""跨站重复岗位识别：单元测试。

这组测试守的是一条很容易在改动中悄悄失效的规则：**只有规范化后完全相等
才算同一岗位**。因为合并错了的后果是「用户永远看不到那个机会」，
比推重复严重得多，所以这里既测「该合并的合了」，也测「不该合并的没被合」。

特别地：
- 同一家公司不同部门的同名岗位（`Data Analyst - Risk` vs `Data Analyst`）
  必须**不合并**；
- 中英文公司名（`恒生银行` vs `Hang Seng Bank`）目前识别不了，这是已知限制，
  用测试把现状钉住，将来真做了对照表再改这条；
- 缺公司名/职位名的岗位一律不参与判断（NO_KEY），不能因为信息不全就猜。

兼容两种运行方式：
- pytest tests/test_dedup.py
- python tests/test_dedup.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.dedup import (
    NO_KEY,
    annotate_duplicates,
    dedupe_key_of,
    normalize_company,
    normalize_title,
)
from hk_job_radar.db import Database
from hk_job_radar.models import JobPosting, now_hk


def _job(source: str, title: str, company: str, uid: str) -> JobPosting:
    return JobPosting(
        source=source, external_id=uid, title=title, company=company,
        url=f"https://example.com/{source}/{uid}",
        url_normalized=f"https://example.com/{source}/{uid}",
    )


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------
def test_company_suffixes_are_stripped():
    same = ["Hang Seng Bank", "Hang Seng Bank Limited", "HANG SENG BANK, LTD.",
            "Hang Seng Bank Ltd"]
    keys = {normalize_company(name) for name in same}
    assert keys == {"hang seng bank"}, keys


def test_company_stacked_suffixes_are_stripped():
    # `xxx co limited` 叠了两个后缀，只剥一层会剩 `xxx co`
    assert normalize_company("Acme Co Limited") == "acme"
    assert normalize_company("Acme Group Holdings Limited") == "acme"


def test_ampersand_equals_and():
    # 招聘网站上 `&` 和 `and` 混用是常态，不统一就永远对不上
    assert normalize_company("Marks & Spencer") == normalize_company("Marks and Spencer")


def test_fullwidth_and_punctuation_are_folded():
    assert normalize_title("ＤＡＴＡ　ＡＮＡＬＹＳＴ") == "data analyst"
    assert normalize_title("Data-Analyst / Analyst.") == "data analyst analyst"


def test_title_strips_reference_numbers():
    assert normalize_title("Data Analyst (Ref: 2024-1234)") == "data analyst"
    assert normalize_title("Data Analyst (Job ID 98765)") == "data analyst"
    assert normalize_title("Data Analyst [Req#A12]") == "data analyst"
    assert normalize_title("Data Analyst（職位編號 1234）") == "data analyst"


def test_title_keeps_department_suffix():
    # ★ 关键：部门后缀区分的是**不同岗位**，剥掉就会把两个岗位当成一个
    assert normalize_title("Data Analyst - Risk") != normalize_title("Data Analyst")
    assert normalize_title("Data Analyst / Credit") != normalize_title("Data Analyst")


def test_key_is_no_key_when_company_or_title_missing():
    assert dedupe_key_of("", "Data Analyst") == NO_KEY
    assert dedupe_key_of("Acme", "") == NO_KEY
    assert dedupe_key_of("Acme", "Data Analyst") == "acme|data analyst"


def test_chinese_and_english_names_do_not_match():
    # 已知限制（见 dedup.py 模块说明）：没有公司名对照表就识别不了。
    # 这条测试是钉住现状用的——将来真的加了对照表，这里要跟着改。
    assert dedupe_key_of("恒生银行", "Data Analyst") != dedupe_key_of("Hang Seng Bank", "Data Analyst")


# ---------------------------------------------------------------------------
# 同批标注
# ---------------------------------------------------------------------------
def test_same_job_on_two_sites_is_marked():
    jobs = [
        _job("jobsdb", "Data Analyst", "Hang Seng Bank Limited", "1"),
        _job("linkedin", "Data Analyst (Ref: 887766)", "Hang Seng Bank", "2"),
    ]
    assert annotate_duplicates(jobs) == 1
    assert jobs[0].duplicate_note == "", "先出现的那条不标注"
    assert "#1" in jobs[1].duplicate_note


def test_different_companies_are_not_merged():
    jobs = [
        _job("jobsdb", "Data Analyst", "HSBC", "1"),
        _job("linkedin", "Data Analyst", "Standard Chartered", "2"),
    ]
    assert annotate_duplicates(jobs) == 0
    assert all(j.duplicate_note == "" for j in jobs)


def test_departments_of_same_company_are_not_merged():
    jobs = [
        _job("jobsdb", "Data Analyst", "Acme Ltd", "1"),
        _job("jobsdb", "Data Analyst - Risk", "Acme Ltd", "2"),
    ]
    assert annotate_duplicates(jobs) == 0, "不同部门的同名岗位是两个岗位，不能合并"


def test_three_sites_mark_the_later_two():
    jobs = [
        _job("jobsdb", "Data Analyst", "Acme", "1"),
        _job("linkedin", "Data Analyst", "Acme", "2"),
        _job("indeed", "Data Analyst", "Acme Limited", "3"),
    ]
    assert annotate_duplicates(jobs) == 2
    assert "#1" in jobs[1].duplicate_note and "#1" in jobs[2].duplicate_note


def test_missing_company_never_marked():
    jobs = [_job("jobsdb", "Data Analyst", "", "1"),
            _job("linkedin", "Data Analyst", "", "2")]
    assert annotate_duplicates(jobs) == 0


# ---------------------------------------------------------------------------
# 数据库层
# ---------------------------------------------------------------------------
def test_upsert_stores_key_and_skips_duplicate_in_realtime():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "t.db"))
        first = _job("jobsdb", "Data Analyst", "Hang Seng Bank Limited", "1")
        second = _job("linkedin", "Data Analyst", "Hang Seng Bank", "2")
        assert db.upsert_job(first) is True
        assert first.dedupe_key == "hang seng bank|data analyst"
        assert db.upsert_job(second) is True, "跨站是两条独立记录（这里只是标记，不是拦截）"

        # 第一条还没推 → 实时推送不拦
        assert db.find_pushed_duplicate(second) is None

        # 第一条推过之后 → 第二条实时推送应当被拦下
        db.mark_reported([first.job_uid])
        found = db.find_pushed_duplicate(second)
        assert found is not None and found.job_uid == first.job_uid

        # 自己推过自己不算重复
        assert db.find_pushed_duplicate(first) is None
        db.close()


def test_backfill_fills_old_rows():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.db"
        db = Database(str(path))
        old = _job("jobsdb", "Data Analyst", "Acme Ltd", "1")
        db.upsert_job(old)
        # 模拟「升级前的老库」：把指纹清空再重开数据库
        with db.conn:
            db.conn.execute("UPDATE jobs SET dedupe_key = ''")
        db.close()

        db2 = Database(str(path))
        row = db2.conn.execute(
            "SELECT dedupe_key FROM jobs WHERE job_uid = ?", (old.job_uid,)).fetchone()
        assert row["dedupe_key"] == "acme|data analyst", "老数据要能被自动补算"
        db2.close()


def test_dedupe_index_exists():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "t.db"))
        names = {r["name"] for r in
                 db.conn.execute("PRAGMA index_list(jobs)").fetchall()}
        # 索引名可能带 sqlite 自动后缀，按前缀判断
        assert any(n.startswith("idx_jobs_dedupe") for n in names), names
        db.close()


# ---------------------------------------------------------------------------
# 卡片渲染
# ---------------------------------------------------------------------------
def test_card_shows_duplicate_note():
    from hk_job_radar.notify.report import build_daily_report

    jobs = [
        JobPosting(source="jobsdb", external_id="1", title="Data Analyst",
                   company="Hang Seng Bank", url="https://x/1", url_normalized="https://x/1",
                   llm_score=80, llm_reason="技能匹配", posted_at=now_hk()),
        JobPosting(source="linkedin", external_id="2", title="Data Analyst",
                   company="Hang Seng Bank Limited", url="https://x/2",
                   url_normalized="https://x/2", llm_score=75, llm_reason="技能匹配",
                   posted_at=now_hk()),
    ]
    pages, _ = build_daily_report(jobs)
    text = "\n".join(pages)
    assert "同一岗位另见 #1" in text, text
    assert "其中 1 个与列表中其他条目是同一岗位" in text


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
