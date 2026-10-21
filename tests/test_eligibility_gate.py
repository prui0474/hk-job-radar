"""资格提示（eligibility note）：只标注、不拦人的那部分。

为什么单独写一组测试：这个功能的**全部价值**在于「程序不替用户做决定」。
一旦哪天有人觉得「反正 PR 岗位投不了，干脆直接过滤掉吧」，这个边界就破了——
有些门槛是硬的（永久居民身份），有些是有余地的（粤语可以边学边投），
只有求职者本人知道能不能试。所以这里用测试把「不影响分数、不影响推送」钉死。

兼容两种运行方式：
- pytest tests/test_eligibility_gate.py
- python tests/test_eligibility_gate.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.db import Database
from hk_job_radar.models import JobPosting, now_hk, to_iso
from hk_job_radar.notify.report import build_html_card, build_job_card
from hk_job_radar.scoring.llm_scorer import LLMScorer, _clean_eligibility


def _job(note: str = "", score: int = 78) -> JobPosting:
    return JobPosting(
        source="jobsdb", external_id="1", title="Data Analyst", company="ACME",
        url="https://example.com/1", url_normalized="https://example.com/1",
        llm_score=score, llm_reason="技能匹配", eligibility_note=note,
    )


# ---------------------------------------------------------------------------
# 一、模型输出规范化
# ---------------------------------------------------------------------------
def test_hard_marker_is_preserved():
    assert _clean_eligibility("[硬] 须為香港永久居民") == "[硬] 须為香港永久居民"


def test_soft_marker_is_preserved():
    assert _clean_eligibility("[软] 要求粵語流利") == "[软] 要求粵語流利"


def test_missing_marker_defaults_to_soft():
    """模型没带前缀 → 按「软」处理。

    宁可提示得轻一点，也不要让程序擅自把一条要求标成「你不可能满足」——
    那等于替用户做了决定。
    """
    assert _clean_eligibility("要求三年以上经验") == "[软] 要求三年以上经验"


def test_fullwidth_brackets_are_understood():
    assert _clean_eligibility("【硬】仅限香港公民") == "[硬] 仅限香港公民"


def test_multiline_and_spaces_are_flattened():
    """卡片上只有一行位置，换行会把版面撑坏。"""
    assert _clean_eligibility("[软] 要求\n  粵語   流利") == "[软] 要求 粵語 流利"


def test_empty_answers_stay_empty():
    assert _clean_eligibility("") == ""
    assert _clean_eligibility("   ") == ""
    assert _clean_eligibility("[硬]") == "", "只有标记没有内容，视为没发现"


def test_overlong_note_is_truncated():
    out = _clean_eligibility("[软] " + "粤" * 100)
    assert out.startswith("[软] ") and out.endswith("…") and len(out) < 60


def test_trailing_parenthesis_is_not_eaten():
    """结尾括号必须原样保留。

    踩过：清洗时用 strip("（）")，把「（需支持本地分行员工）」的右括号一起剥掉，
    卡片上就成了半截括号。改成只剥开头。
    """
    raw = "[软] 要求粵語流利（需支持本地分行员工）"
    assert _clean_eligibility(raw) == raw


def test_two_markers_in_one_answer_are_merged():
    """模型偶尔一条里塞两种标记，卡片上不能出现两个图标。"""
    out = _clean_eligibility("[硬] 须為永久居民；[软] 需3年经验")
    assert out == "[硬] 须為永久居民；需3年经验"


# ---------------------------------------------------------------------------
# 二、解析：资格提示绝不能动分数
# ---------------------------------------------------------------------------
def test_parse_reads_eligibility_without_touching_score():
    raw = ('{"score": 82, "reason": "技能吻合", '
           '"eligibility": "[软] 要求粵語流利"}')
    result = LLMScorer._parse_response(raw)
    assert result is not None
    assert result.score == 82, "资格提示不该影响分数"
    assert result.eligibility == "[软] 要求粵語流利"


def test_parse_tolerates_missing_eligibility_field():
    """老版本 prompt / 模型漏字段时不能降级成「未打分」。"""
    result = LLMScorer._parse_response('{"score": 70, "reason": "还行"}')
    assert result is not None and result.score == 70 and result.eligibility == ""


# ---------------------------------------------------------------------------
# 三、卡片渲染：两种标记强度不同，但都照常出现在日报里
# ---------------------------------------------------------------------------
def test_hard_note_shows_up_with_a_stop_sign():
    card = build_job_card(_job("[硬] 须為香港永久居民"))
    assert "🚫 门槛：须為香港永久居民" in card


def test_soft_note_shows_up_with_a_warning_sign():
    card = build_job_card(_job("[软] 要求粵語流利"))
    assert "⚠️ 门槛：要求粵語流利" in card


def test_no_note_leaves_no_extra_line():
    card = build_job_card(_job(""))
    assert "门槛" not in card


def test_legacy_note_without_marker_is_still_shown():
    assert "⚠️ 门槛：要求三年经验" in build_job_card(_job("要求三年经验"))


def test_html_card_carries_the_note_too():
    """邮件通道不能漏——用户可能只看邮件。"""
    hard = build_html_card(_job("[硬] 须為香港永久居民"))
    assert "身份门槛" in hard and "须為香港永久居民" in hard
    soft = build_html_card(_job("[软] 要求粵語流利"))
    assert "条件门槛" in soft and "要求粵語流利" in soft


# ---------------------------------------------------------------------------
# 四、入库：新字段要能存能取，老库要能自动补列
# ---------------------------------------------------------------------------
def test_note_round_trips_through_the_database():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "t.db"))
        job = _job("[软] 要求粵語流利")
        db.upsert_job(job)
        db.update_score(job)
        loaded = db.get_job(job.job_uid)
        assert loaded is not None and loaded.eligibility_note == "[软] 要求粵語流利"
        db.close()


def test_old_database_gets_the_new_column():
    """老用户的库是旧 schema，CREATE TABLE IF NOT EXISTS 不会补列。

    不补的话读岗位直接报 "no such column"，等于升级即崩——这条守的是升级路径。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        raw = sqlite3.connect(path)
        raw.execute("""
            CREATE TABLE jobs (
                job_uid TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
                title TEXT NOT NULL, company TEXT NOT NULL, url TEXT NOT NULL,
                url_normalized TEXT NOT NULL, salary_text TEXT DEFAULT '',
                location TEXT DEFAULT '', posted_at TEXT,
                description_snippet TEXT DEFAULT '', keyword_matched TEXT DEFAULT '',
                llm_score INTEGER, llm_reason TEXT DEFAULT '',
                direct_apply INTEGER DEFAULT 0, status TEXT DEFAULT 'new',
                first_seen_at TEXT NOT NULL, reported INTEGER DEFAULT 0,
                UNIQUE(source, external_id), UNIQUE(url_normalized)
            )""")
        raw.execute(
            "INSERT INTO jobs (job_uid, source, external_id, title, company, url,"
            " url_normalized, first_seen_at) VALUES"
            " ('jobsdb:1','jobsdb','1','T','C','u','u',?)",
            (to_iso(now_hk()),))
        raw.commit()
        raw.close()

        db = Database(str(path))          # 这一步应当自动补列
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(jobs)")}
        assert "eligibility_note" in cols, "老库没有被补列"
        loaded = db.get_job("jobsdb:1")
        assert loaded is not None and loaded.eligibility_note == ""
        db.close()


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
                passed += 1
            except Exception as exc:                       # noqa: BLE001
                print(f"FAIL  {name}: {exc}")
                failed += 1
    print(f"\n{passed} tests passed" + (f", {failed} FAILED" if failed else ""))
    sys.exit(1 if failed else 0)
